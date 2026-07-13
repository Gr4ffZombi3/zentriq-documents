from datetime import datetime, timezone

from app.extensions import db


class Customer(db.Model):
    __tablename__ = "customers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False, index=True)
    address = db.Column(db.String(255))
    city = db.Column(db.String(120), index=True)
    postal_code = db.Column(db.String(20), index=True)
    date_of_birth = db.Column(db.Date, nullable=True)
    email = db.Column(db.String(255), nullable=True)
    phone = db.Column(db.String(50), nullable=True)

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    documents = db.relationship("Document", back_populates="customer")
    recommendations = db.relationship("Recommendation", back_populates="customer")

    def __repr__(self):
        return f"<Customer {self.id} {self.name!r}>"
