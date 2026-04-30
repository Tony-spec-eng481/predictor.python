from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import uuid

db = SQLAlchemy()

class AviatorRound(db.Model):
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