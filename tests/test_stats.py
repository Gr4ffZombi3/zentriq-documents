from app.models import Customer, DocStatus, Document, Recommendation
from app.models.enums import DocType, Priority, RecommendationStatus, RecommendationType
from app.services.stats import get_dashboard_stats, get_open_recommendations


def test_dashboard_stats_counts_by_status_and_type(db, tenant):
    db.session.add_all(
        [
            Document(
                filename="a.pdf", original_filename="a.pdf", file_path="/tmp/a.pdf",
                status=DocStatus.DONE, doc_type=DocType.RECHNUNG, tenant_id=tenant.id,
            ),
            Document(
                filename="b.pdf", original_filename="b.pdf", file_path="/tmp/b.pdf",
                status=DocStatus.DONE, doc_type=DocType.RECHNUNG, tenant_id=tenant.id,
            ),
            Document(
                filename="c.pdf", original_filename="c.pdf", file_path="/tmp/c.pdf",
                status=DocStatus.FAILED, tenant_id=tenant.id,
            ),
            Document(
                filename="d.pdf", original_filename="d.pdf", file_path="/tmp/d.pdf",
                status=DocStatus.PENDING, tenant_id=tenant.id,
            ),
        ]
    )
    db.session.commit()

    stats = get_dashboard_stats()
    assert stats["total_documents"] == 4
    assert stats["done_count"] == 2
    assert stats["failed_count"] == 1
    assert stats["new_documents"] == 4  # alle gerade erst erstellt
    assert stats["doc_type_counts"][DocType.RECHNUNG] == 2


def test_open_recommendations_sorted_by_priority_desc(db, tenant):
    document = Document(
        filename="x.pdf", original_filename="x.pdf", file_path="/tmp/x.pdf", tenant_id=tenant.id
    )
    customer = Customer(name="Test Kunde", tenant_id=tenant.id)
    db.session.add_all([document, customer])
    db.session.commit()

    low = Recommendation(
        document=document, customer=customer, type=RecommendationType.OTHER,
        label="Niedrig", priority=Priority.LOW, tenant_id=tenant.id,
    )
    high = Recommendation(
        document=document, customer=customer, type=RecommendationType.CALL_TODAY,
        label="Hoch", priority=Priority.HIGH, tenant_id=tenant.id,
    )
    dismissed = Recommendation(
        document=document, customer=customer, type=RecommendationType.OTHER,
        label="Erledigt", priority=Priority.HIGH, status=RecommendationStatus.DISMISSED,
        tenant_id=tenant.id,
    )
    db.session.add_all([low, high, dismissed])
    db.session.commit()

    results = get_open_recommendations()
    assert [r.label for r in results] == ["Hoch", "Niedrig"]
