from core.db import CompositeScore, SessionLocal, create_tables


def test_create_tables_and_insert():
    create_tables()
    session = SessionLocal()
    with session.begin():
        record = CompositeScore(
            flow_score=50,
            oi_score=50,
            volume_score=50,
            manipulation_score=50,
            regulatory_score=50,
            overall_score=50,
        )
        session.add(record)
    with session.begin():
        stored = session.query(CompositeScore).first()
        assert stored is not None
        assert stored.overall_score == 50
