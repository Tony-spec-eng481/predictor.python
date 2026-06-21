from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import uuid

db = SQLAlchemy()

class RoadWorxRound(db.Model):
    __tablename__ = 'road_worx_round'
    id = db.Column(db.Integer, primary_key=True)
    multiplier = db.Column(db.Float, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    session_id = db.Column(db.String(36), default=lambda: str(uuid.uuid4()))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Provably fair fields (nullable — populated when available from WebSocket)
    game_round_id = db.Column(db.String(64), nullable=True)    # Spribe's internal round ID
    server_seed_hash = db.Column(db.String(128), nullable=True) # committed before round
    server_seed = db.Column(db.String(128), nullable=True)      # revealed after round ends
    client_seed_1 = db.Column(db.String(128), nullable=True)
    client_seed_2 = db.Column(db.String(128), nullable=True)
    client_seed_3 = db.Column(db.String(128), nullable=True)
    nonce = db.Column(db.Integer, nullable=True)
    sha512_hash = db.Column(db.String(128), nullable=True)      # full verification hash
    source = db.Column(db.String(16), default='websocket')      # 'websocket' | 'dom' | 'manual'

    def to_dict(self):
        return {
            'id': self.id,
            'multiplier': round(self.multiplier, 2),
            'timestamp': self.timestamp.isoformat(),
            'session_id': self.session_id,
            'game_round_id': self.game_round_id,
            'server_seed_hash': self.server_seed_hash,
            'server_seed': self.server_seed,
            'client_seed_1': self.client_seed_1,
            'client_seed_2': self.client_seed_2,
            'client_seed_3': self.client_seed_3,
            'nonce': self.nonce,
            'sha512_hash': self.sha512_hash,
            'source': self.source or 'websocket',
        }

    def to_summary_dict(self):
        """Lightweight dict for list views (omits full seed data)."""
        return {
            'id': self.id,
            'multiplier': round(self.multiplier, 2),
            'timestamp': self.timestamp.isoformat(),
            'session_id': self.session_id,
            'game_round_id': self.game_round_id,
            'source': self.source or 'websocket',
            'has_seeds': bool(self.server_seed),
        }


class AIBetLog(db.Model):
    """Tracks every AI betting decision and outcome."""
    __tablename__ = 'ai_bet_log'

    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    action = db.Column(db.String(16))                  # 'bet', 'skip', 'stop_session'
    stake = db.Column(db.Float, nullable=True)
    target_multiplier = db.Column(db.Float, nullable=True)
    actual_multiplier = db.Column(db.Float, nullable=True)
    profit_loss = db.Column(db.Float, nullable=True)
    balance_before = db.Column(db.Float, nullable=True)
    balance_after = db.Column(db.Float, nullable=True)
    confidence = db.Column(db.Float, nullable=True)
    risk_level = db.Column(db.String(16), nullable=True)
    reasoning = db.Column(db.Text, nullable=True)
    analysis_snapshot = db.Column(db.Text, nullable=True)   # JSON string
    outcome = db.Column(db.String(16), nullable=True)       # 'win', 'loss', 'skipped', 'error'
    session_id = db.Column(db.String(36), nullable=True)
    is_dry_run = db.Column(db.Boolean, default=True)

    def to_dict(self):
        return {
            'id': self.id,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
            'action': self.action,
            'stake': round(self.stake, 2) if self.stake else None,
            'target_multiplier': self.target_multiplier,
            'actual_multiplier': round(self.actual_multiplier, 2) if self.actual_multiplier else None,
            'profit_loss': round(self.profit_loss, 2) if self.profit_loss is not None else None,
            'balance_before': round(self.balance_before, 2) if self.balance_before else None,
            'balance_after': round(self.balance_after, 2) if self.balance_after else None,
            'confidence': self.confidence,
            'risk_level': self.risk_level,
            'reasoning': self.reasoning,
            'outcome': self.outcome,
            'session_id': self.session_id,
            'is_dry_run': self.is_dry_run,
        }