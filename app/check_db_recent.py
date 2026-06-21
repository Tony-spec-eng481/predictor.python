from app import app, db, RoadWorxRound
import json

with app.app_context():
    rounds = RoadWorxRound.query.order_by(RoadWorxRound.id.desc()).limit(10).all()
    print(json.dumps([r.to_dict() for r in rounds], indent=2))
