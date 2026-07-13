import re
import uuid


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or uuid.uuid4().hex[:8]


def unique_tenant_slug(base: str) -> str:
    from app.models import Tenant

    slug = slugify(base)
    candidate = slug
    suffix = 1
    while Tenant.query.filter_by(slug=candidate).first() is not None:
        suffix += 1
        candidate = f"{slug}-{suffix}"
    return candidate
