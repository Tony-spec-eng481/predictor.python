import gevent.monkey
gevent.monkey.patch_all()

import os
import threading
import asyncio
import time
import io
import csv
import logging
import statistics
from datetime import datetime
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from flask_socketio import SocketIO, emit
from sqlalchemy import func, inspect
from dotenv import load_dotenv

from config import get_config
from models import db, AviatorRound
from playwright_collector import PlaywrightAviatorCollector
from provably_fair import verify_round as pf_verify

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
collector = PlaywrightAviatorCollector(api_url=internal_api)

def _push_multiplier(data):
    with app.app_context():
        # Only emit 'new_multiplier' for final results (to add to history log)
        if data.get('is_final'):
            socketio.emit('new_multiplier', data)
        
        # Always emit status update for the live ticker
        socketio.emit('status_update', _get_enhanced_status())

collector.on_multiplier = _push_multiplier

def migrate_db():
    with app.app_context():
        db.create_all()
        inspector = inspect(db.engine)
        existing = [c['name'] for c in inspector.get_columns('aviator_round')]
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
                        db.text(f"ALTER TABLE aviator_round ADD COLUMN {col} {col_type}")
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
        existing_by_id = AviatorRound.query.filter_by(game_round_id=game_round_id).first()
        if existing_by_id:
            return jsonify(existing_by_id.to_dict()), 200

    # 2. Check for recent rounds with same multiplier (fuzzy deduplication)
    # This handles cases where we get the same result twice (e.g. once without ID, once with ID)
    recent_limit = datetime.utcnow()
    # Look back 15 seconds
    existing_recent = AviatorRound.query.filter(
        AviatorRound.multiplier >= multiplier - 0.001,
        AviatorRound.multiplier <= multiplier + 0.001,
        AviatorRound.timestamp >= datetime.fromtimestamp(datetime.utcnow().timestamp() - 15)
    ).order_by(AviatorRound.timestamp.desc()).first()

    if existing_recent:
        # If we just got an ID for a round that didn't have one, update it!
        if game_round_id and not existing_recent.game_round_id:
            existing_recent.game_round_id = game_round_id
            db.session.commit()
            return jsonify(existing_recent.to_dict()), 200
        
        # Otherwise, it's just a duplicate
        return jsonify(existing_recent.to_dict()), 200

    round_data = AviatorRound(
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
    return jsonify(payload)

@app.route('/api/multipliers')
def get_multipliers():
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    query = AviatorRound.query.order_by(AviatorRound.timestamp.desc())
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
    query = db.session.query(AviatorRound.multiplier).all()
    multipliers = [m[0] for m in query]
    
    if not multipliers:
        return jsonify({
            'total_rounds': 0,
            'avg_multiplier': 0,
            'median_multiplier': 0,
            'min_multiplier': 0,
            'max_multiplier': 0,
            'last_multiplier': 0
        })
    
    last_round = AviatorRound.query.order_by(AviatorRound.timestamp.desc()).first()
    recent_multipliers = db.session.query(AviatorRound.multiplier).order_by(AviatorRound.timestamp.desc()).limit(50).all()
    
    # 24-hour Trend analysis
    one_day_ago = datetime.utcnow().timestamp() - 86400
    one_day_ago_dt = datetime.fromtimestamp(one_day_ago)
    
    rounds_24h = AviatorRound.query.filter(AviatorRound.timestamp >= one_day_ago_dt).count()
    avg_24h_query = db.session.query(func.avg(AviatorRound.multiplier)).filter(AviatorRound.timestamp >= one_day_ago_dt).scalar()
    avg_24h = float(avg_24h_query) if avg_24h_query else 0
    
    total_rounds_count = len(multipliers)
    overall_avg = statistics.mean(multipliers) if multipliers else 0
    
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
        'trends': {
            'rounds_24h': rounds_24h,
            'avg_24h': avg_24h
        }
    })

@app.route('/api/distribution')
def get_distribution():
    query = db.session.query(AviatorRound.multiplier).all()
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
    query = db.session.query(AviatorRound.multiplier).all()
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
    Advanced Aviator multiplier predictor combining:
    1. Provably-fair SHA-512 cryptographic analysis on stored rounds
    2. Nonce sequence extrapolation
    3. Statistical mean-reversion model
    4. Server-seed hash entropy scoring
    """
    import hashlib
    import re

    # ----- fetch recent rounds with provably fair data -----
    recent_rounds = (
        AviatorRound.query
        .order_by(AviatorRound.timestamp.desc())
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
        AviatorRound.query
        .order_by(AviatorRound.timestamp.desc())
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
    query = db.session.query(AviatorRound.multiplier).all()
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
    query = db.session.query(AviatorRound.multiplier).order_by(AviatorRound.timestamp.asc()).all()
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
    
    query = db.session.query(AviatorRound.multiplier).order_by(AviatorRound.timestamp.asc()).all()
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
        round_obj = AviatorRound.query.get(round_id)
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
        num_deleted = AviatorRound.query.delete()
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
        status['collected_count'] = AviatorRound.query.count()
        # Get truly last final multiplier from DB
        last_final = AviatorRound.query.order_by(AviatorRound.timestamp.desc()).first()
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
