from app import app, db, AviatorRound
import json

with app.app_context():
    rounds = AviatorRound.query.order_by(AviatorRound.id.desc()).limit(10).all()
    print(json.dumps([r.to_dict() for r in rounds], indent=2))
