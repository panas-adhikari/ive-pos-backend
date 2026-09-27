import asyncio
import logging

from sqlalchemy import delete, select

from app.auth.models import AuditEvent, Invitation, Membership, Organization
from app.auth.security import now
from app.operations.models import Customer, Product, Sale, SaleLine, StockBalance, StockMovement
from app.stores.models import Register, Store

logger = logging.getLogger(__name__)


async def purge_organization(db, organization_id) -> bool:
    """Delete tenant-owned records while retaining shared user identities."""
    organization = await db.scalar(
        select(Organization).where(Organization.id == organization_id).with_for_update()
    )
    if organization is None:
        return False
    store_ids = select(Store.id).where(Store.organization_id == organization_id)
    sale_ids = select(Sale.id).where(Sale.organization_id == organization_id)
    await db.execute(delete(StockMovement).where(StockMovement.organization_id == organization_id))
    await db.execute(delete(SaleLine).where(SaleLine.sale_id.in_(sale_ids)))
    await db.execute(delete(Sale).where(Sale.organization_id == organization_id))
    await db.execute(delete(StockBalance).where(StockBalance.organization_id == organization_id))
    await db.execute(delete(Customer).where(Customer.organization_id == organization_id))
    await db.execute(delete(Product).where(Product.organization_id == organization_id))
    await db.execute(delete(Register).where(Register.store_id.in_(store_ids)))
    await db.execute(delete(Invitation).where(Invitation.organization_id == organization_id))
    await db.execute(delete(AuditEvent).where(AuditEvent.organization_id == organization_id))
    await db.execute(delete(Membership).where(Membership.organization_id == organization_id))
    await db.execute(delete(Store).where(Store.organization_id == organization_id))
    await db.delete(organization)
    return True


async def purge_due_organizations(session_factory) -> int:
    async with session_factory() as db, db.begin():
        organization_ids = list(
            await db.scalars(
                select(Organization.id).where(
                    Organization.deletion_scheduled_for.is_not(None),
                    Organization.deletion_scheduled_for <= now(),
                )
            )
        )
        deleted = 0
        for organization_id in organization_ids:
            deleted += await purge_organization(db, organization_id)
        return deleted


async def deletion_worker(session_factory) -> None:
    while True:
        await asyncio.sleep(60)
        try:
            await purge_due_organizations(session_factory)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Scheduled organization deletion pass failed; retrying later")
