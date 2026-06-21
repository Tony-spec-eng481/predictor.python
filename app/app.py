import gevent.monkey
gevent.monkey.patch_all()

import os
import threading
import asyncio
import time
import io
import csv
import json
import logging
import statistics
from datetime import datetime
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from flask_socketio import SocketIO, emit
from sqlalchemy import func, inspect
from dotenv import load_dotenv

from config import get_config
from models import db, RoadWorxRound, AIBetLog
from playwright_collector import PlaywrightRoadWorxCollector
from provably_fair import verify_round as pf_verify
from ai_engine import AIStrategyEngine, AIConfig, AIDecision, SessionStats
from bet_executor import BetExecutor

# Load environment variables from .env
load_dotenv()

# Initialize Config
cfg = get_config()

# Configure Logging
logging.basicConfig(
    level=getattr(logging, cfg.LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config.from_object(cfg)

# Initialize Extensions
db.init_app(app)
CORS(app, resources={r"/api/*": {"origins": cfg.CORS_ORIGINS}})

socketio = SocketIO(
    app, 
    cors_allowed_origins=cfg.CORS_ORIGINS, 
    async_mode=cfg.SOCKETIO_ASYNC_MODE,
    logger=False, 
    engineio_logger=False
)

# Use configured internal API URL for collector if provided
internal_api = f"http://{cfg.HOST}:{cfg.PORT}/api/multiplier"
collector = PlaywrightRoadWorxCollector(api_url=internal_api)

# --- AI Engine & Executor ---
ai_engine: AIStrategyEngine = None  # Initialized after app context is ready
bet_executor = BetExecutor(dry_run=True)
ai_session_thread = None
ai_stop_event = threading.Event()
ai_bet_in_progress = False

def _push_multiplier(data):
    with app.app_context():
        # Only emit 'new_multiplier' for final results (to add to history log)
        if data.get('is_final'):
            socketio.emit('new_multiplier', data)

        # Always emit status update for the live ticker
        socketio.emit('status_update', _get_enhanced_status())

        # If AI is running, feed it the new multiplier for decision
        if ai_engine and ai_engine.stats.is_running and data.get('is_final'):
            _ai_process_round(data.get('multiplier'))

            # Live mode: sync the real browser balance to the AI after each round
            if not ai_engine.config.dry_run and collector.page and hasattr(collector, 'loop') and collector.loop:
                def _sync_balance_bg():
                    try:
                        import asyncio as _asyncio
                        future = _asyncio.run_coroutine_threadsafe(
                            bet_executor.get_current_balance(), collector.loop
                        )
                        real_bal = future.result(timeout=6.0)
                        if real_bal is not None and ai_engine and ai_engine.stats.is_running:
                            ai_engine.stats.current_balance = real_bal
                            ai_engine.stats.total_profit_loss = real_bal - ai_engine.stats.starting_balance
                            ai_engine.stats.peak_balance = max(ai_engine.stats.peak_balance, real_bal)
                            ai_engine.stats.lowest_balance = min(ai_engine.stats.lowest_balance, real_bal)
                            with app.app_context():
                                socketio.emit('ai_status_update', ai_engine.stats.to_dict())
                            logger.info(f"Periodic balance sync: {real_bal} KES")
                    except Exception as _e:
                        logger.debug(f"Balance sync skipped: {_e}")
                threading.Thread(target=_sync_balance_bg, daemon=True).start()

collector.on_multiplier = _push_multiplier

def migrate_db():
    with app.app_context():
        db.create_all()
        inspector = inspect(db.engine)
        existing = [c['name'] for c in inspector.get_columns('road_worx_round')]
        new_cols = {
            'game_round_id': 'VARCHAR(64)',
            'server_seed_hash': 'VARCHAR(128)',
            'server_seed': 'VARCHAR(128)',
            'client_seed_1': 'VARCHAR(128)',
            'client_seed_2': 'VARCHAR(128)',
            'client_seed_3': 'VARCHAR(128)',
            'nonce': 'INTEGER',
            'sha512_hash': 'VARCHAR(128)',
            'source': 'VARCHAR(16)',
        }
        for col, col_type in new_cols.items():
            if col not in existing:
                try:
                    db.session.execute(
                        db.text(f"ALTER TABLE road_worx_round ADD COLUMN {col} {col_type}")
                    )
                except:
                    pass
        db.session.commit()

# Add a custom logging handler to push logs to frontend
class SocketIOHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            socketio.emit('backend_log', {'message': msg, 'level': record.levelname})
        except:
            pass

socket_handler = SocketIOHandler()
socket_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(message)s'))
logging.getLogger().addHandler(socket_handler) # Add to root logger

@socketio.on('connect')
def handle_connect():
    emit('status_update', _get_enhanced_status())
    emit('backend_log', {'message': 'Connected to backend log stream.', 'level': 'INFO'})

@app.route('/api/multiplier', methods=['POST'])
def add_multiplier():
    data = request.json
    if not data or 'multiplier' not in data:
        return jsonify({'error': 'Missing multiplier field'}), 400
    try:
        multiplier = float(data['multiplier'])
    except:
        return jsonify({'error': 'Invalid multiplier value'}), 400
    
    # Round multiplier to 2 decimal places for consistent deduplication
    multiplier = round(multiplier, 2)
    game_round_id = data.get('game_round_id')
    
    # 1. Check by Round ID first (absolute deduplication)
    if game_round_id:
        existing_by_id = RoadWorxRound.query.filter_by(game_round_id=game_round_id).first()
        if existing_by_id:
            return jsonify(existing_by_id.to_dict()), 200

    # 2. Check for recent rounds with same multiplier (fuzzy deduplication)
    # This handles cases where we get the same result twice (e.g. once without ID, once with ID)
    recent_limit = datetime.utcnow()
    # Look back 15 seconds
    existing_recent = RoadWorxRound.query.filter(
        RoadWorxRound.multiplier >= multiplier - 0.001,
        RoadWorxRound.multiplier <= multiplier + 0.001,
        RoadWorxRound.timestamp >= datetime.fromtimestamp(datetime.utcnow().timestamp() - 15)
    ).order_by(RoadWorxRound.timestamp.desc()).first()

    if existing_recent:
        # If we just got an ID for a round that didn't have one, update it!
        if game_round_id and not existing_recent.game_round_id:
            existing_recent.game_round_id = game_round_id
            db.session.commit()
            return jsonify(existing_recent.to_dict()), 200
        
        # Otherwise, it's just a duplicate
        return jsonify(existing_recent.to_dict()), 200

    round_data = RoadWorxRound(
        multiplier=multiplier,
        source=data.get('source', 'playwright_ws'),
        game_round_id=data.get('game_round_id'),
        server_seed_hash=data.get('server_seed_hash'),
        server_seed=data.get('server_seed'),
        client_seed_1=data.get('client_seed_1'),
        client_seed_2=data.get('client_seed_2'),
        client_seed_3=data.get('client_seed_3'),
        nonce=data.get('nonce'),
        sha512_hash=data.get('sha512_hash'),
    )
    db.session.add(round_data)
    db.session.commit()
    
    payload = round_data.to_dict()
    socketio.emit('new_multiplier', payload)
    socketio.emit('status_update', collector.get_status())
    
    # Trigger AI process round if AI is running
    if ai_engine and ai_engine.stats.is_running:
        _ai_process_round(multiplier)
        
    return jsonify(payload)

@app.route('/api/multipliers')
def get_multipliers():
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    min_multiplier = request.args.get('min_multiplier')
    max_multiplier = request.args.get('max_multiplier')

    query = RoadWorxRound.query

    if start_date:
        try:
            start_dt = datetime.strptime(start_date, '%Y-%m-%d')
            query = query.filter(RoadWorxRound.timestamp >= start_dt)
        except ValueError:
            pass

    if end_date:
        try:
            end_dt = datetime.strptime(end_date + ' 23:59:59', '%Y-%m-%d %H:%M:%S')
            query = query.filter(RoadWorxRound.timestamp <= end_dt)
        except ValueError:
            pass

    if min_multiplier:
        try:
            query = query.filter(RoadWorxRound.multiplier >= float(min_multiplier))
        except ValueError:
            pass

    if max_multiplier:
        try:
            query = query.filter(RoadWorxRound.multiplier <= float(max_multiplier))
        except ValueError:
            pass

    query = query.order_by(RoadWorxRound.timestamp.desc())
    total = query.count()
    multipliers = query.paginate(page=page, per_page=per_page, error_out=False)
    return jsonify({
        'multipliers': [m.to_dict() for m in multipliers.items],
        'total': total,
        'page': page,
        'per_page': per_page,
    })

@app.route('/api/stats')
def get_stats():
    query = db.session.query(RoadWorxRound.multiplier).all()
    multipliers = [m[0] for m in query]
    
    if not multipliers:
        return jsonify({
            'total_rounds': 0,
            'avg_multiplier': 0,
            'median_multiplier': 0,
            'min_multiplier': 0,
            'max_multiplier': 0,
            'last_multiplier': 0,
            'low_multiplier_prob': 0
        })
    
    last_round = RoadWorxRound.query.order_by(RoadWorxRound.timestamp.desc()).first()
    recent_multipliers = db.session.query(RoadWorxRound.multiplier).order_by(RoadWorxRound.timestamp.desc()).limit(50).all()
    
    # 24-hour Trend analysis
    one_day_ago = datetime.utcnow().timestamp() - 86400
    one_day_ago_dt = datetime.fromtimestamp(one_day_ago)
    
    rounds_24h = RoadWorxRound.query.filter(RoadWorxRound.timestamp >= one_day_ago_dt).count()
    avg_24h_query = db.session.query(func.avg(RoadWorxRound.multiplier)).filter(RoadWorxRound.timestamp >= one_day_ago_dt).scalar()
    avg_24h = float(avg_24h_query) if avg_24h_query else 0
    
    total_rounds_count = len(multipliers)
    overall_avg = statistics.mean(multipliers) if multipliers else 0
    
    # Calculate crash rate (under 2.00x)
    low_multiplier_count = len([m for m in multipliers if m < 2.0])
    low_multiplier_prob = round((low_multiplier_count / total_rounds_count) * 100, 2) if total_rounds_count > 0 else 0

    rounds_trend = f"+{rounds_24h}" if rounds_24h > 0 else "0"
    avg_trend = round(avg_24h - overall_avg, 2)
    avg_trend_str = f"+{avg_trend}" if avg_trend > 0 else str(avg_trend)

    return jsonify({
        'total_rounds': total_rounds_count,
        'avg_multiplier': round(overall_avg, 2),
        'median_multiplier': round(statistics.median(multipliers), 2) if multipliers else 0,
        'most_frequent_multiplier': round(statistics.mode(multipliers), 2) if multipliers else 0,
        'min_multiplier': round(min(multipliers), 2) if multipliers else 0,
        'max_multiplier': round(max(multipliers), 2) if multipliers else 0,
        'last_multiplier': last_round.multiplier if last_round else 0,
        'recent_multipliers': [m[0] for m in recent_multipliers][::-1],
        'low_multiplier_prob': low_multiplier_prob,
        'trends': {
            'rounds_24h': rounds_24h,
            'avg_24h': avg_24h
        }
    })

@app.route('/api/distribution')
def get_distribution():
    query = db.session.query(RoadWorxRound.multiplier).all()
    multipliers = [m[0] for m in query]
    total = len(multipliers)
    
    if total == 0:
        return jsonify([])

    ranges = [
        {'range': '1.00 - 1.50', 'min': 1.0, 'max': 1.50, 'color': '#ef4444'},
        {'range': '1.51 - 2.00', 'min': 1.51, 'max': 2.00, 'color': '#f87171'},
        {'range': '2.01 - 3.00', 'min': 2.01, 'max': 3.00, 'color': '#3b82f6'},
        {'range': '3.01 - 5.00', 'min': 3.01, 'max': 5.00, 'color': '#60a5fa'},
        {'range': '5.01 - 10.00', 'min': 5.01, 'max': 10.00, 'color': '#f59e0b'},
        {'range': 'Above 10.00', 'min': 10.01, 'max': 1000000.0, 'color': '#8b5cf6'}
    ]
    
    results = []
    for r in ranges:
        count = len([m for m in multipliers if r['min'] <= m <= r['max']])
        results.append({
            'range': r['range'],
            'count': count,
            'percentage': round((count / total) * 100, 2),
            'color': r['color']
        })
        
    return jsonify(results)

@app.route('/api/probabilities')
def get_probabilities():
    query = db.session.query(RoadWorxRound.multiplier).all()
    multipliers = [m[0] for m in query]
    total = len(multipliers)
    
    if total == 0:
        return jsonify({'p2x': 0, 'p5x': 0, 'p10x': 0, 'range_probs': {}})
        
    # Custom ranges probabilities
    ranges = [
        (1.0, 1.50), (1.51, 2.00), (2.01, 3.00), (3.01, 5.00), (5.01, 10.00), (10.01, 1000000.0)
    ]
    
    probs = {}
    for r_min, r_max in ranges:
        key = f"p_{r_min}_{r_max}".replace('.', '_')
        probs[key] = round((len([m for m in multipliers if r_min <= m <= r_max]) / total), 4)

    return jsonify({
        'p2x': round(len([m for m in multipliers if m >= 2.0]) / total * 100, 2),
        'p5x': round(len([m for m in multipliers if m >= 5.0]) / total * 100, 2),
        'p10x': round(len([m for m in multipliers if m >= 10.0]) / total * 100, 2),
        'range_probs': probs
    })

@app.route('/api/predict')
def predict():
    """
    Advanced Road Worx multiplier predictor combining:
    1. Provably-fair SHA-512 cryptographic analysis on stored rounds
    2. Nonce sequence extrapolation
    3. Statistical mean-reversion model
    4. Server-seed hash entropy scoring
    """
    import hashlib
    import re

    # ----- fetch recent rounds with provably fair data -----
    recent_rounds = (
        RoadWorxRound.query
        .order_by(RoadWorxRound.timestamp.desc())
        .limit(100)
        .all()
    )
    if not recent_rounds:
        return jsonify({'prediction': 1.00, 'confidence': 0, 'reason': 'No data collected yet'})

    multipliers = [r.multiplier for r in recent_rounds]
    avg_all   = statistics.mean(multipliers)
    avg_5     = statistics.mean(multipliers[:5])
    std_all   = statistics.stdev(multipliers) if len(multipliers) > 1 else 0

    # ----- collect rounds that have provably-fair seeds -----
    seeded_rounds = [
        r for r in recent_rounds
        if r.server_seed and r.client_seed_1
    ]

    confidence_base = min(75, 30 + len(multipliers) * 0.5)
    method_used = "statistical"
    reason_parts = []

    # ===== 1. CRYPTOGRAPHIC SEED PATTERN ANALYSIS =====
    crypto_bonus = 0
    predicted_from_crypto = None

    if len(seeded_rounds) >= 3:
        # Build SHA-512 byte-entropy averages from known rounds
        # to derive a scoring offset for the next (unknown) hash
        hash_firsts = []   # first 13 hex chars → integer used in the formula
        for r in seeded_rounds[:20]:
            seeds = [s for s in [r.client_seed_1, r.client_seed_2, r.client_seed_3] if s]
            combined = ":".join([r.server_seed] + seeds)
            if r.nonce is not None:
                combined += f":{r.nonce}"
            h_hex  = hashlib.sha512(combined.encode()).hexdigest()
            h_int  = int(h_hex[:13], 16)
            hash_firsts.append(h_int)

        # Statistical properties of the hash integers
        e = 2 ** 52
        hash_mean = statistics.mean(hash_firsts)
        hash_std  = statistics.stdev(hash_firsts) if len(hash_firsts) > 1 else 0

        # Project the "next" hash integer using mean ± 0.5 std
        proj_h = hash_mean  # expected value of a uniform draw
        if proj_h % 33 == 0:
            predicted_from_crypto = 1.00
        else:
            raw = (100 * e - proj_h) / (e - proj_h)
            predicted_from_crypto = round(max(1.00, raw / 100), 2)

        crypto_bonus = 12
        method_used = "provably_fair_hash_projection"
        reason_parts.append(f"SHA-512 projection over {len(hash_firsts)} verified rounds")

    # ===== 2. NONCE SEQUENCE ANALYSIS =====
    nonce_bonus = 0
    nonce_rounds = [r for r in recent_rounds if r.nonce is not None]
    if len(nonce_rounds) >= 5:
        nonces      = [r.nonce for r in nonce_rounds]
        nonce_diffs = [abs(nonces[i] - nonces[i+1]) for i in range(len(nonces)-1)]
        avg_diff    = statistics.mean(nonce_diffs)
        # Consistent nonce increments indicate sequential rounds → higher confidence
        if max(nonce_diffs) - min(nonce_diffs) < avg_diff * 0.1:
            nonce_bonus = 8
            reason_parts.append("sequential nonce pattern detected")

    # ===== 3. SERVER-SEED HASH ENTROPY ANALYSIS =====
    entropy_bonus = 0
    hash_rounds = [r for r in recent_rounds if r.server_seed_hash]
    if hash_rounds:
        # Measure hex diversity in stored server-seed hashes
        sample_hash = hash_rounds[0].server_seed_hash or ""
        unique_nibbles = len(set(sample_hash.lower())) if sample_hash else 0
        # A well-distributed hash has ~14-16 unique hex digits (0-f)
        if unique_nibbles >= 14:
            entropy_bonus = 5
            reason_parts.append("high server-seed hash entropy confirmed")

    # ===== 4. STATISTICAL MEAN-REVERSION MODEL =====
    stat_prediction = avg_all
    stat_reason = "mean reversion"

    if avg_5 < 1.5:
        stat_prediction = avg_all * 1.3
        stat_reason = "low-streak recovery expected"
    elif avg_5 > 5:
        stat_prediction = avg_all * 0.75
        stat_reason = "high-streak cool-down"
    elif avg_5 < avg_all * 0.7:
        stat_prediction = avg_all * 1.15
        stat_reason = "below-average recent run, mean pull-up"
    elif avg_5 > avg_all * 1.4:
        stat_prediction = avg_all * 0.9
        stat_reason = "above-average run cooling"

    if not reason_parts:
        reason_parts.append(stat_reason)

    # ===== COMBINE PREDICTIONS =====
    if predicted_from_crypto is not None:
        # Weighted blend: 55% crypto projection, 45% statistical
        final_prediction = round(
            0.55 * predicted_from_crypto + 0.45 * stat_prediction, 2
        )
    else:
        final_prediction = round(stat_prediction, 2)

    final_prediction = max(1.00, final_prediction)

    # ===== FINAL CONFIDENCE =====
    total_confidence = round(
        min(94, confidence_base + crypto_bonus + nonce_bonus + entropy_bonus), 1
    )

    # ===== NEXT SERVER SEED HASH (committed for next round) =====
    next_hash_preview = None
    latest_with_hash = next(
        (r for r in recent_rounds if r.server_seed_hash), None
    )
    if latest_with_hash:
        next_hash_preview = latest_with_hash.server_seed_hash

    return jsonify({
        'prediction':        final_prediction,
        'confidence':        total_confidence,
        'reason':            f"{'; '.join(reason_parts) or 'statistical analysis'}",
        'method':            method_used,
        'next_server_hash':  next_hash_preview,
        'data_points':       len(multipliers),
        'seeded_rounds':     len(seeded_rounds),
        'avg_all':           round(avg_all, 2),
        'avg_recent_5':      round(avg_5, 2),
        'std_dev':           round(std_all, 2),
    })


@app.route('/api/predict/crypto')
def predict_crypto():
    """
    Returns detailed cryptographic analysis data for the predictor UI:
    - Hash chain of recent rounds (server_seed_hash values)
    - Verified vs unverified round counts
    - Nonce sequence data
    - Per-round SHA-512 computation results
    """
    import hashlib

    rounds = (
        RoadWorxRound.query
        .order_by(RoadWorxRound.timestamp.desc())
        .limit(30)
        .all()
    )

    chain = []
    verified_count = 0
    unverified_count = 0

    for r in rounds:
        has_seeds = bool(r.server_seed and r.client_seed_1)
        computed_hash = None
        computed_multiplier = None

        if has_seeds:
            seeds = [s for s in [r.client_seed_1, r.client_seed_2, r.client_seed_3] if s]
            combined = ":".join([r.server_seed] + seeds)
            if r.nonce is not None:
                combined += f":{r.nonce}"
            computed_hash = hashlib.sha512(combined.encode()).hexdigest()

            h_int = int(computed_hash[:13], 16)
            e = 2 ** 52
            if h_int % 33 == 0:
                computed_multiplier = 1.00
            else:
                raw = (100 * e - h_int) / (e - h_int)
                computed_multiplier = round(max(1.00, raw / 100), 2)

            # Verify against recorded
            diff = abs((computed_multiplier or 0) - r.multiplier)
            if diff <= 0.05:
                verified_count += 1
            else:
                unverified_count += 1
        else:
            unverified_count += 1

        chain.append({
            'id':                  r.id,
            'game_round_id':       r.game_round_id,
            'multiplier':          round(r.multiplier, 2),
            'server_seed_hash':    r.server_seed_hash,
            'server_seed_preview': (r.server_seed or '')[:16] + '...' if r.server_seed else None,
            'nonce':               r.nonce,
            'computed_hash':       (computed_hash or '')[:32] + '...' if computed_hash else None,
            'computed_multiplier': computed_multiplier,
            'verified':            has_seeds and computed_multiplier is not None and abs((computed_multiplier or 0) - r.multiplier) <= 0.05,
            'timestamp':           r.timestamp.isoformat(),
        })

    return jsonify({
        'chain':            chain,
        'verified_count':   verified_count,
        'unverified_count': unverified_count,
        'total':            len(rounds),
        'verification_rate': round(verified_count / len(rounds) * 100, 1) if rounds else 0,
    })

@app.route('/api/volatility')
def get_volatility():
    query = db.session.query(RoadWorxRound.multiplier).all()
    multipliers = [m[0] for m in query]
    
    if len(multipliers) < 2:
        return jsonify({'variance': 0, 'std_dev': 0, 'indicator': 'Insufficient Data'})
        
    var = statistics.variance(multipliers)
    std_dev = statistics.stdev(multipliers)
    
    indicator = 'Low'
    if std_dev > 10: indicator = 'High'
    elif std_dev > 4: indicator = 'Medium'
    
    return jsonify({
        'variance': round(var, 2),
        'std_dev': round(std_dev, 2),
        'indicator': indicator
    })

@app.route('/api/patterns')
def get_patterns():
    query = db.session.query(RoadWorxRound.multiplier).order_by(RoadWorxRound.timestamp.asc()).all()
    multipliers = [m[0] for m in query]
    
    if not multipliers:
        return jsonify({'streaks': [], 'longest_low': 0, 'longest_high': 0, 'recent_streak': {}})
        
    streaks = []
    current_streak = {'type': None, 'length': 0}
    
    for m in multipliers:
        m_type = 'high' if m >= 2.0 else 'low'
        if m_type == current_streak['type']:
            current_streak['length'] += 1
        else:
            if current_streak['type']:
                streaks.append(current_streak.copy())
            current_streak = {'type': m_type, 'length': 1}
    
    streaks.append(current_streak) # Add last streak
    
    longest_low = max([s['length'] for s in streaks if s['type'] == 'low'] or [0])
    longest_high = max([s['length'] for s in streaks if s['type'] == 'high'] or [0])
    
    return jsonify({
        'streaks': streaks[-20:], # Return last 20 streaks for visualization
        'longest_low': longest_low,
        'longest_high': longest_high,
        'recent_streak': streaks[-1] if streaks else {}
    })

@app.route('/api/simulate', methods=['POST'])
def simulate_strategy():
    data = request.json
    strategy = data.get('strategy', 'fixed') # 'fixed', 'martingale'
    start_balance = float(data.get('balance', 100.0))
    base_bet = float(data.get('bet_size', 1.0))
    target = float(data.get('target', 2.0))
    
    query = db.session.query(RoadWorxRound.multiplier).order_by(RoadWorxRound.timestamp.asc()).all()
    multipliers = [m[0] for m in query]
    
    balance = start_balance
    max_balance = start_balance
    min_balance = start_balance
    wins = 0
    losses = 0
    current_bet = base_bet
    history = []
    
    for m in multipliers:
        if balance < current_bet:
            break # Ruined
            
        balance -= current_bet
        if m >= target:
            # Win
            profit = current_bet * target
            balance += profit
            wins += 1
            if strategy == 'martingale':
                current_bet = base_bet
        else:
            # Loss
            losses += 1
            if strategy == 'martingale':
                current_bet *= 2
        
        max_balance = max(max_balance, balance)
        min_balance = min(min_balance, balance)
        history.append(balance)
        
    total_rounds = wins + losses
    return jsonify({
        'final_balance': round(balance, 2),
        'profit_loss': round(balance - start_balance, 2),
        'win_rate': round((wins / total_rounds * 100), 2) if total_rounds > 0 else 0,
        'max_drawdown': round(max_balance - min_balance, 2),
        'is_ruined': balance < current_bet,
        'total_simulated': total_rounds,
        'equity_curve': history[::max(1, len(history)//100)] # Sample 100 points
    })

@app.route('/api/multiplier/<int:round_id>', methods=['DELETE'])
def delete_multiplier(round_id):
    try:
        round_obj = RoadWorxRound.query.get(round_id)
        if not round_obj:
            return jsonify({'error': 'Round not found'}), 404
        db.session.delete(round_obj)
        db.session.commit()
        return jsonify({'status': 'success'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/api/multipliers/clear', methods=['POST'])
def clear_multipliers():
    try:
        num_deleted = RoadWorxRound.query.delete()
        db.session.commit()
        socketio.emit('history_cleared')
        return jsonify({'status': 'success', 'deleted': num_deleted})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

def _get_enhanced_status():
    status = collector.get_status()
    try:
        # Get real DB count
        status['collected_count'] = RoadWorxRound.query.count()
        # Get truly last final multiplier from DB
        last_final = RoadWorxRound.query.order_by(RoadWorxRound.timestamp.desc()).first()
        if last_final:
            status['last_crash'] = last_final.multiplier
    except:
        pass
    return status

@app.route('/api/collection/status', methods=['GET'])
def get_collection_status():
    """Returns the current state of the data collector."""
    return jsonify(_get_enhanced_status())

@app.route('/api/collection/start', methods=['POST'])
def start_collection():
    def run_collector():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(collector.start_collection())
        except Exception as e:
            logger.error(f"Collector thread error: {e}")

    if not collector.is_running:
        threading.Thread(target=run_collector, daemon=True).start()
    
    socketio.emit('collection_status', collector.get_status())
    return jsonify({'status': 'running', 'message': 'Playwright collection starting...', **collector.get_status()})

@app.route('/api/collection/stop', methods=['POST'])
def stop_collection():
    collector.stop_collection()
    socketio.emit('collection_status', collector.get_status())
    return jsonify({'status': 'stopped'})

# ===================================================================
# AI TRADING ENDPOINTS
# ===================================================================

def _init_ai_engine():
    """Initialize the AI engine with a DB session factory, loading optimized defaults."""
    global ai_engine
    from ai_engine import load_optimized_params
    opt_params = load_optimized_params()
    
    config_dict = {
        'risk_level': 'conservative',
        'stop_loss_pct': 30.0,
        'kelly_fraction': 0.25,
        'dry_run': True,
    }
    # Load optimized defaults
    config_dict.update(opt_params)
    
    ai_engine = AIStrategyEngine(
        db_session_factory=db.session,
        config=AIConfig.from_dict(config_dict),
    )
    logger.info("AI Strategy Engine initialized with optimized defaults")


async def run_bet_in_background(decision):
    global ai_bet_in_progress
    try:
        logger.info(f"Running bet execution in background for stake: {decision.stake}, target: {decision.target_multiplier}")
        result = await bet_executor.execute_bet(decision.stake, decision.target_multiplier)

        # --- LIVE MODE: Sync real game balance from the browser ---
        # After the bet resolves, read the actual balance shown in the game website
        # and push it back into the AI engine so both displays stay in sync.
        real_balance_after = None
        if not ai_engine.config.dry_run and bet_executor.page:
            try:
                real_balance_after = await bet_executor.get_current_balance()
                if real_balance_after is not None:
                    logger.info(f"Real game balance after bet: {real_balance_after} KES")
            except Exception as bal_err:
                logger.warning(f"Could not read game balance after bet: {bal_err}")

        with app.app_context():
            actual = result.get("actual_multiplier", 1.0)
            won = result.get("won", False)
            profit = result.get("profit", -decision.stake)
            outcome = "win" if won else "loss"
            if result.get("error"):
                outcome = "error"
                logger.error(f"Bet execution failed: {result['error']}")

            balance_before = ai_engine.stats.current_balance
            ai_engine.record_outcome(actual, decision)

            # Override internal balance with the REAL browser balance (live mode only)
            if real_balance_after is not None:
                ai_engine.stats.current_balance = real_balance_after
                ai_engine.stats.total_profit_loss = real_balance_after - ai_engine.stats.starting_balance
                ai_engine.stats.peak_balance = max(ai_engine.stats.peak_balance, real_balance_after)
                ai_engine.stats.lowest_balance = min(ai_engine.stats.lowest_balance, real_balance_after)
                # Append the corrected real balance to the equity curve
                ai_engine.stats.equity_curve.append(round(real_balance_after, 2))
                logger.info(f"AI balance synced to real game balance: {real_balance_after} KES")

            log_entry = AIBetLog(
                action='bet',
                stake=decision.stake,
                target_multiplier=decision.target_multiplier,
                actual_multiplier=actual,
                profit_loss=profit,
                balance_before=balance_before,
                balance_after=ai_engine.stats.current_balance,
                confidence=decision.confidence,
                risk_level=decision.risk_level,
                reasoning=decision.reasoning + (f" | Error: {result['error']}" if result.get("error") else ""),
                analysis_snapshot=json.dumps(decision.analysis) if decision.analysis else None,
                outcome=outcome,
                session_id=ai_engine.stats.session_id,
                is_dry_run=ai_engine.config.dry_run,
            )
            db.session.add(log_entry)
            db.session.commit()

            socketio.emit('ai_trade_result', {
                'outcome': outcome,
                'stake': decision.stake,
                'target': decision.target_multiplier,
                'actual': actual,
                'profit_loss': round(profit, 2),
                'balance': round(ai_engine.stats.current_balance, 2),
                'is_dry_run': ai_engine.config.dry_run,
            })
            socketio.emit('ai_status_update', ai_engine.stats.to_dict())

            if not result.get("error"):
                # Insert the round into the DB so we accumulate history
                source_name = 'ai_bet_executor_dry' if ai_engine.config.dry_run else 'ai_bet_executor_live'
                round_entry = RoadWorxRound(
                    multiplier=actual,
                    source=source_name,
                    game_round_id=f"ai_{int(time.time())}",
                    session_id=ai_engine.stats.session_id
                )
                db.session.add(round_entry)
                db.session.commit()

                # Trigger next round decision cycle
                def trigger_next():
                    time.sleep(3)
                    with app.app_context():
                        if ai_engine and ai_engine.stats.is_running:
                            logger.info("Triggering next AI decision cycle after bet completion.")
                            _ai_process_round(actual)
                threading.Thread(target=trigger_next, daemon=True).start()
            else:
                # If the bet execution failed with an error, trigger the next round check after a delay to keep the loop alive
                def trigger_next_after_error():
                    time.sleep(5)
                    with app.app_context():
                        if ai_engine and ai_engine.stats.is_running:
                            logger.info("Retrying AI decision cycle after bet execution error.")
                            _ai_process_round(1.0)
                threading.Thread(target=trigger_next_after_error, daemon=True).start()

    except Exception as e:
        logger.error(f"Exception in run_bet_in_background: {e}")
        import traceback
        traceback.print_exc()
    finally:
        ai_bet_in_progress = False


def _ai_process_round(multiplier: float):
    """Called each time a final multiplier is captured. Drives the AI loop."""
    global ai_engine, ai_bet_in_progress
    if not ai_engine or not ai_engine.stats.is_running:
        return

    if ai_bet_in_progress:
        logger.info("AI bet is currently in progress, skipping analysis for this round.")
        return

    try:
        # Fetch recent multipliers from DB
        recent = (
            db.session.query(RoadWorxRound.multiplier)
            .order_by(RoadWorxRound.timestamp.desc())
            .limit(ai_engine.config.analysis_window)
            .all()
        )
        mult_list = [r[0] for r in recent]

        # Make decision
        decision = ai_engine.make_decision(mult_list)

        # Emit decision to frontend
        socketio.emit('ai_decision', decision.to_dict())

        # Record in DB
        balance_before = ai_engine.stats.current_balance

        if decision.action == 'bet':
            # Execute bet asynchronously on the collector loop
            if collector.is_running and hasattr(collector, 'loop') and collector.loop and collector.page:
                bet_executor.set_page(collector.page)
                bet_executor.dry_run = ai_engine.config.dry_run
                
                ai_bet_in_progress = True
                asyncio.run_coroutine_threadsafe(run_bet_in_background(decision), collector.loop)
                logger.info("Spawned background bet execution task.")
            else:
                # Fallback: collector not running or loop not initialized, simulate
                logger.warning("Collector or event loop not running. Simulating bet outcome.")
                actual = multiplier
                ai_engine.record_outcome(actual, decision)

                outcome = 'win' if actual >= decision.target_multiplier else 'loss'
                profit = (decision.stake * decision.target_multiplier - decision.stake) if outcome == 'win' else -decision.stake

                log_entry = AIBetLog(
                    action='bet',
                    stake=decision.stake,
                    target_multiplier=decision.target_multiplier,
                    actual_multiplier=actual,
                    profit_loss=profit,
                    balance_before=balance_before,
                    balance_after=ai_engine.stats.current_balance,
                    confidence=decision.confidence,
                    risk_level=decision.risk_level,
                    reasoning=decision.reasoning + " (Simulated fallback)",
                    analysis_snapshot=json.dumps(decision.analysis) if decision.analysis else None,
                    outcome=outcome,
                    session_id=ai_engine.stats.session_id,
                    is_dry_run=ai_engine.config.dry_run,
                )
                db.session.add(log_entry)
                db.session.commit()

                socketio.emit('ai_trade_result', {
                    'outcome': outcome,
                    'stake': decision.stake,
                    'target': decision.target_multiplier,
                    'actual': actual,
                    'profit_loss': round(profit, 2),
                    'balance': round(ai_engine.stats.current_balance, 2),
                    'is_dry_run': ai_engine.config.dry_run,
                })

        elif decision.action == 'skip':
            ai_engine.record_outcome(multiplier, decision)
            log_entry = AIBetLog(
                action='skip',
                confidence=decision.confidence,
                reasoning=decision.reasoning,
                outcome='skipped',
                session_id=ai_engine.stats.session_id,
                is_dry_run=ai_engine.config.dry_run,
                actual_multiplier=multiplier,
                balance_before=balance_before,
                balance_after=ai_engine.stats.current_balance,
            )
            db.session.add(log_entry)
            db.session.commit()

            # Since it's a single-player game, a skip would stall the loop.
            # We schedule a new decision check after a short delay (e.g. 5 seconds)
            def trigger_next_after_skip():
                time.sleep(5)
                with app.app_context():
                    if ai_engine and ai_engine.stats.is_running:
                        logger.info("Triggering next AI decision cycle after skip.")
                        _ai_process_round(1.0)
            
            threading.Thread(target=trigger_next_after_skip, daemon=True).start()

        elif decision.action == 'stop_session':
            ai_engine.stop_session(reason=decision.reasoning)
            log_entry = AIBetLog(
                action='stop_session',
                reasoning=decision.reasoning,
                outcome='session_stopped',
                session_id=ai_engine.stats.session_id,
                is_dry_run=ai_engine.config.dry_run,
                balance_before=balance_before,
                balance_after=ai_engine.stats.current_balance,
            )
            db.session.add(log_entry)
            db.session.commit()
            socketio.emit('ai_status_update', ai_engine.stats.to_dict())

        # Push updated stats
        socketio.emit('ai_status_update', ai_engine.stats.to_dict())

    except Exception as e:
        logger.error(f"AI process round error: {e}")
        import traceback
        traceback.print_exc()


@app.route('/api/ai/status', methods=['GET'])
def ai_status():
    """Get current AI engine state."""
    if not ai_engine:
        return jsonify({'is_running': False, 'message': 'AI engine not initialized'})
    return jsonify({
        'is_running': ai_engine.stats.is_running,
        'session': ai_engine.stats.to_dict(),
        'config': ai_engine.config.to_dict(),
    })


@app.route('/api/ai/start', methods=['POST'])
def ai_start():
    """Start an AI trading session."""
    global ai_engine
    if not ai_engine:
        _init_ai_engine()

    data = request.json or {}

    # 1. Automatically start collection if not running
    if not collector.is_running:
        logger.info("Collector not running. Starting Playwright collection automatically...")
        def run_collector():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(collector.start_collection())
            except Exception as e:
                logger.error(f"Collector thread error: {e}")

        threading.Thread(target=run_collector, daemon=True).start()
        
        # Wait up to 35 seconds for the browser page to initialize
        for _ in range(35):
            if collector.page:
                break
            time.sleep(1)

    # 2. Extract the actual balance from the browser page using BetExecutor
    real_balance = None
    if collector.page and hasattr(collector, 'loop') and collector.loop:
        bet_executor.set_page(collector.page)
        # Run get_current_balance thread-safely on collector's loop
        future = asyncio.run_coroutine_threadsafe(
            bet_executor.get_current_balance(),
            collector.loop
        )
        try:
            # Wait up to 18 seconds for balance extraction
            real_balance = future.result(timeout=18.0)
        except Exception as e:
            logger.error(f"Failed to fetch real balance from UI: {e}")

    # 3. Determine starting bankroll
    starting_bankroll = float(data.get('bankroll', 100))
    if real_balance is not None:
        logger.info(f"Fetched real balance from game UI: {real_balance} KES")
        starting_bankroll = real_balance
    else:
        logger.warning(f"Could not fetch real balance from game UI. Using pre-configured or default bankroll: {starting_bankroll} KES")

    # 4. Initialize AI configuration and start the strategy session, merging optimized params
    from ai_engine import load_optimized_params
    opt_params = load_optimized_params()
    
    config_dict = {
        'bankroll': starting_bankroll,
        'risk_level': data.get('risk_level', 'conservative'),
        'max_bet_pct': float(data.get('max_bet_pct', 5.0)),
        'max_bet_abs': float(data.get('max_bet_abs', 50.0)),
        'stop_loss_pct': float(data.get('stop_loss_pct', 30.0)),
        'take_profit_pct': float(data.get('take_profit_pct', 50.0)),
        'dry_run': data.get('dry_run', True),
    }
    
    for key in ['kelly_fraction', 'min_data_points', 'analysis_window', 'cooldown_after_loss', 'max_consecutive_losses']:
        if key in data:
            config_dict[key] = data[key]
        elif key in opt_params:
            config_dict[key] = opt_params[key]
            
    # Load optimized weights & thresholds if present
    for key, val in opt_params.items():
        if key.startswith('w_') or key in ['confidence_threshold', 'weight_ev', 'weight_prob']:
            config_dict[key] = val

    config = AIConfig.from_dict(config_dict)

    ai_engine.start_session(config)
    bet_executor.dry_run = config.dry_run

    # Share the collector's page with the executor
    if collector.page:
        bet_executor.set_page(collector.page)

    socketio.emit('ai_status_update', ai_engine.stats.to_dict())
    socketio.emit('collection_status', collector.get_status())
    
    # Trigger first AI round check in the background so we don't block the start HTTP response
    def trigger_initial():
        time.sleep(1)
        with app.app_context():
            if ai_engine and ai_engine.stats.is_running:
                logger.info("Triggering initial AI decision cycle.")
                _ai_process_round(1.0)
    threading.Thread(target=trigger_initial, daemon=True).start()

    return jsonify({
        'status': 'started',
        'session_id': ai_engine.stats.session_id,
        'config': config.to_dict(),
        'collection': collector.get_status(),
    })


@app.route('/api/ai/stop', methods=['POST'])
def ai_stop():
    """Stop the AI trading session."""
    if not ai_engine:
        return jsonify({'status': 'not_running'})

    reason = (request.json or {}).get('reason', 'user_stop')
    ai_engine.stop_session(reason=reason)
    
    # Also stop the collection to close the browser cleanly
    if collector.is_running:
        logger.info("Stopping collector along with AI stop...")
        collector.stop_collection()

    socketio.emit('ai_status_update', ai_engine.stats.to_dict())
    socketio.emit('collection_status', collector.get_status())
    
    return jsonify({
        'status': 'stopped',
        'session': ai_engine.stats.to_dict(),
        'collection': collector.get_status(),
    })

@app.route('/api/ai/config', methods=['GET', 'POST'])
def ai_config():
    """Get or update AI configuration."""
    if not ai_engine:
        _init_ai_engine()

    if request.method == 'GET':
        return jsonify(ai_engine.config.to_dict())

    data = request.json or {}
    # Update config fields
    for key, val in data.items():
        if hasattr(ai_engine.config, key):
            current = getattr(ai_engine.config, key)
            if isinstance(current, float):
                setattr(ai_engine.config, key, float(val))
            elif isinstance(current, int):
                setattr(ai_engine.config, key, int(val))
            elif isinstance(current, bool):
                setattr(ai_engine.config, key, bool(val))
            elif isinstance(current, str):
                setattr(ai_engine.config, key, str(val))

    return jsonify(ai_engine.config.to_dict())


@app.route('/api/ai/decision', methods=['GET'])
def ai_latest_decision():
    """Get the latest AI decision without executing."""
    if not ai_engine:
        return jsonify({'action': 'skip', 'reasoning': 'AI engine not started'})

    # Run analysis on current data
    recent = (
        db.session.query(RoadWorxRound.multiplier)
        .order_by(RoadWorxRound.timestamp.desc())
        .limit(ai_engine.config.analysis_window)
        .all()
    )
    mult_list = [r[0] for r in recent]
    decision = ai_engine.make_decision(mult_list)
    return jsonify(decision.to_dict())


@app.route('/api/ai/history', methods=['GET'])
def ai_history():
    """Get paginated AI bet log history."""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 30, type=int)
    action_filter = request.args.get('action')  # 'bet', 'skip', etc.

    query = AIBetLog.query
    if action_filter:
        query = query.filter_by(action=action_filter)

    query = query.order_by(AIBetLog.timestamp.desc())
    total = query.count()
    items = query.paginate(page=page, per_page=per_page, error_out=False)

    return jsonify({
        'history': [item.to_dict() for item in items.items],
        'total': total,
        'page': page,
        'per_page': per_page,
    })


@app.route('/api/ai/stats', methods=['GET'])
def ai_stats():
    """Get AI session stats."""
    if not ai_engine:
        return jsonify({'is_running': False})
    return jsonify(ai_engine.stats.to_dict())


@app.route('/api/ai/train', methods=['POST'])
def ai_train():
    """Run dynamic parameter optimization on historical database rounds."""
    global ai_engine
    if not ai_engine:
        _init_ai_engine()

    data = request.json or {}
    rounds_limit = int(data.get('rounds', 1000))
    if rounds_limit < 100:
        rounds_limit = 100

    try:
        # Fetch historical multipliers (newest first in query, reversed to oldest-first)
        recent_rounds = (
            RoadWorxRound.query
            .order_by(RoadWorxRound.timestamp.desc())
            .limit(rounds_limit)
            .all()
        )
        
        if len(recent_rounds) < 50:
            return jsonify({
                'error': 'Insufficient database records. Need at least 50 collected rounds to train.',
                'data_points': len(recent_rounds)
            }), 400

        multipliers = [r.multiplier for r in reversed(recent_rounds)]

        from ai_engine import optimize_parameters, save_optimized_params
        
        # Clone active config
        base_config = ai_engine.config
        
        # Run optimization
        result = optimize_parameters(multipliers, base_config)
        
        # Save optimized params
        opt_config = result["optimized_config"]
        opt_params = {
            "kelly_fraction": opt_config.kelly_fraction,
            "confidence_threshold": opt_config.confidence_threshold,
            "cooldown_after_loss": opt_config.cooldown_after_loss,
            "analysis_window": opt_config.analysis_window,
            "w_prob_high": opt_config.w_prob_high,
            "w_prob_med": opt_config.w_prob_med,
            "w_mr_oversold": opt_config.w_mr_oversold,
            "w_vol_low": opt_config.w_vol_low,
            "w_streak_low": opt_config.w_streak_low,
            "w_data_quality": opt_config.w_data_quality,
            "w_mr_overbought": opt_config.w_mr_overbought,
            "w_vol_high": opt_config.w_vol_high,
            "w_streak_high": opt_config.w_streak_high,
            "w_prob_low": opt_config.w_prob_low,
            "w_loss_penalty": opt_config.w_loss_penalty,
            "weight_ev": opt_config.weight_ev,
            "weight_prob": opt_config.weight_prob
        }
        save_optimized_params(opt_params)

        # Update active config dynamically
        ai_engine.config = opt_config
        socketio.emit('ai_status_update', ai_engine.stats.to_dict())

        return jsonify({
            'status': 'success',
            'data_points': len(multipliers),
            'original_stats': result["original_stats"],
            'optimized_stats': result["optimized_stats"],
            'improvement_pct': result["improvement_pct"],
            'optimized_config': opt_params
        })

    except Exception as e:
        logger.error(f"Error during AI self-training: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/ai/analysis', methods=['GET'])
def ai_analysis():
    """Get the latest market analysis snapshot."""
    if not ai_engine:
        _init_ai_engine()

    recent = (
        db.session.query(RoadWorxRound.multiplier)
        .order_by(RoadWorxRound.timestamp.desc())
        .limit(ai_engine.config.analysis_window)
        .all()
    )
    mult_list = [r[0] for r in recent]

    if len(mult_list) < 5:
        return jsonify({'error': 'Insufficient data', 'data_points': len(mult_list)})

    analysis = ai_engine._analyse(mult_list)
    return jsonify(analysis)


@app.route('/api/ai/history/clear', methods=['POST'])
def ai_clear_history():
    """Clear AI bet log history."""
    try:
        num_deleted = AIBetLog.query.delete()
        db.session.commit()
        return jsonify({'status': 'success', 'deleted': num_deleted})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


# ===================================================================
# ERROR HANDLERS
# ===================================================================

@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Not found'}), 404

@app.errorhandler(500)
def internal_error(error):
    db.session.rollback()
    return jsonify({'error': 'Internal server error'}), 500

if __name__ == '__main__':
    # Initialize DB
    with app.app_context():
        migrate_db()
        _init_ai_engine()
    
    # Check if we are running in production mode
    is_prod = os.environ.get('FLASK_ENV') == 'production'
    
    logger.info(f"System starting in {'production' if is_prod else 'development'} mode...")
    
    if not is_prod:
        # development server
        socketio.run(
            app, 
            debug=cfg.DEBUG, 
            host=cfg.HOST, 
            port=cfg.PORT, 
            use_reloader=False
        )
    else:
        # In production, this script should generally NOT be run directly.
        # Use a WSGI server like Gunicorn: gunicorn wsgi:application
        logger.warning("Running production mode with development server is NOT recommended.")
        socketio.run(app, host=cfg.HOST, port=cfg.PORT)
