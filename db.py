import os
from datetime import datetime, timedelta

from sqlalchemy import (
    BigInteger, Integer, String, Text, Boolean, DateTime,
    ForeignKey, select, func, update, text
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Integer
    id = __import__("sqlalchemy").Column(Integer, primary_key=True)
    tg_id = __import__("sqlalchemy").Column(BigInteger, unique=True, index=True, nullable=False)
    name = __import__("sqlalchemy").Column(String(64), nullable=False)
    age = __import__("sqlalchemy").Column(Integer, nullable=False)
    city = __import__("sqlalchemy").Column(String(64), nullable=False)
    bio = __import__("sqlalchemy").Column(Text, nullable=True)
    photo_file_id = __import__("sqlalchemy").Column(String(256), nullable=False)
    gender = __import__("sqlalchemy").Column(String(16), nullable=False)  # male/female
    rate_pref = __import__("sqlalchemy").Column(String(16), default="both")  # who I want to rate
    be_rated_by = __import__("sqlalchemy").Column(String(16), default="both")  # who can rate me
    photo_version = __import__("sqlalchemy").Column(Integer, default=1)
    blocked = __import__("sqlalchemy").Column(Boolean, default=False)
    blocked_at = __import__("sqlalchemy").Column(DateTime, nullable=True)
    last_active_at = __import__("sqlalchemy").Column(DateTime, nullable=True)
    created_at = __import__("sqlalchemy").Column(DateTime, default=datetime.utcnow)

class Rating(Base):
    __tablename__ = "ratings"
    id = __import__("sqlalchemy").Column(Integer, primary_key=True)
    rater_tg_id = __import__("sqlalchemy").Column(BigInteger, index=True, nullable=False)
    rated_tg_id = __import__("sqlalchemy").Column(BigInteger, index=True, nullable=False)
    score = __import__("sqlalchemy").Column(Integer, nullable=False)
    message = __import__("sqlalchemy").Column(String(500), nullable=True)
    rated_photo_version = __import__("sqlalchemy").Column(Integer, default=1)
    seen = __import__("sqlalchemy").Column(Boolean, default=False, index=True)
    created_at = __import__("sqlalchemy").Column(DateTime, default=datetime.utcnow)

class RequiredChannel(Base):
    __tablename__ = "required_channels"
    id = __import__("sqlalchemy").Column(Integer, primary_key=True)
    username = __import__("sqlalchemy").Column(String(128), nullable=True)
    title = __import__("sqlalchemy").Column(String(128), nullable=True)
    link = __import__("sqlalchemy").Column(String(256), nullable=True)
    is_active = __import__("sqlalchemy").Column(Boolean, default=True, index=True)
    created_at = __import__("sqlalchemy").Column(DateTime, default=datetime.utcnow)

class Report(Base):
    __tablename__ = "reports"
    id = __import__("sqlalchemy").Column(Integer, primary_key=True)
    reporter_tg_id = __import__("sqlalchemy").Column(BigInteger, index=True, nullable=False)
    reported_tg_id = __import__("sqlalchemy").Column(BigInteger, index=True, nullable=False)
    rated_tg_id = __import__("sqlalchemy").Column(BigInteger, nullable=True)
    text = __import__("sqlalchemy").Column(Text, nullable=True)
    created_at = __import__("sqlalchemy").Column(DateTime, default=datetime.utcnow)
    is_reviewed = __import__("sqlalchemy").Column(Boolean, default=False, index=True)

def create_engine(db_url: str):
    return create_async_engine(db_url, echo=False, pool_pre_ping=True, pool_size=10, max_overflow=30)

def create_sessionmaker(engine):
    return sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def init_db(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        if conn.dialect.name == "postgresql":
            # columns migrations
            await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS be_rated_by VARCHAR(16) DEFAULT 'both'"))
            await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS photo_version INTEGER DEFAULT 1"))
            await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS blocked BOOLEAN DEFAULT FALSE"))
            await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS blocked_at TIMESTAMP NULL"))
            await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active_at TIMESTAMP NULL"))
            await conn.execute(text("ALTER TABLE ratings ADD COLUMN IF NOT EXISTS rated_photo_version INTEGER DEFAULT 1"))
            await conn.execute(text("ALTER TABLE ratings ADD COLUMN IF NOT EXISTS seen BOOLEAN DEFAULT FALSE"))

            # drop any legacy constraints/indexes that block rerate
            await conn.execute(text("ALTER TABLE ratings DROP CONSTRAINT IF EXISTS uq_rater_rated"))
            await conn.execute(text("ALTER TABLE ratings DROP CONSTRAINT IF EXISTS uq_rater_rated_version"))
            await conn.execute(text("DROP INDEX IF EXISTS uq_rater_rated"))
            await conn.execute(text("DROP INDEX IF EXISTS uq_rater_rated_version"))
            await conn.execute(text("DROP INDEX IF EXISTS uq_rater_rated_unseen"))

            # create correct partial unique index
            await conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_rater_rated_unseen "
                "ON ratings (rater_tg_id, rated_tg_id) WHERE seen IS false"
            ))

            # helpful indexes
            await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_users_last_active ON users (last_active_at DESC)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_ratings_rated_seen ON ratings (rated_tg_id, seen)"))

# --------- helpers ----------
async def get_user_by_tg_id(session: AsyncSession, tg_id: int):
    res = await session.execute(select(User).where(User.tg_id == tg_id))
    return res.scalar_one_or_none()

async def create_user(session: AsyncSession, **kwargs):
    u = User(**kwargs, created_at=datetime.utcnow(), last_active_at=datetime.utcnow())
    session.add(u)
    await session.commit()
    return u

async def touch_activity(session: AsyncSession, tg_id: int):
    await session.execute(update(User).where(User.tg_id == tg_id).values(last_active_at=datetime.utcnow()))
    await session.commit()

async def get_unseen_count(session: AsyncSession, tg_id: int) -> int:
    res = await session.execute(select(func.count(Rating.id)).where(Rating.rated_tg_id==tg_id, Rating.seen.is_(False)))
    return int(res.scalar() or 0)

async def get_rating_stats(session: AsyncSession, tg_id: int):
    res = await session.execute(select(func.avg(Rating.score), func.count(Rating.id)).where(Rating.rated_tg_id==tg_id))
    avg_, cnt_ = res.one()
    return float(avg_ or 0), int(cnt_ or 0)

async def save_rating(session: AsyncSession, rater_tg_id: int, rated_tg_id: int, rated_photo_version: int, score: int, message: str | None):
    # try insert; if conflicts with unseen unique, update existing unseen record (prevents crash)
    r = Rating(
        rater_tg_id=rater_tg_id,
        rated_tg_id=rated_tg_id,
        score=score,
        message=message,
        rated_photo_version=rated_photo_version,
        seen=False,
        created_at=datetime.utcnow()
    )
    session.add(r)
    try:
        await session.execute(update(User).where(User.tg_id==rater_tg_id).values(last_active_at=datetime.utcnow()))
        await session.commit()
        return r
    except IntegrityError:
        await session.rollback()
        # update existing unseen rating (works even if legacy unique remains)
        await session.execute(
            update(Rating)
            .where(Rating.rater_tg_id==rater_tg_id, Rating.rated_tg_id==rated_tg_id)
            .values(score=score, message=message, rated_photo_version=rated_photo_version, seen=False, created_at=datetime.utcnow())
        )
        await session.execute(update(User).where(User.tg_id==rater_tg_id).values(last_active_at=datetime.utcnow()))
        await session.commit()
        return None

async def mark_ratings_seen(session: AsyncSession, rated_tg_id: int, rating_ids: list[int]):
    if not rating_ids: 
        return
    await session.execute(update(Rating).where(Rating.id.in_(rating_ids), Rating.rated_tg_id==rated_tg_id).values(seen=True))
    await session.commit()

async def fetch_unseen_ratings(session: AsyncSession, tg_id: int, limit: int = 5):
    res = await session.execute(
        select(Rating).where(Rating.rated_tg_id==tg_id, Rating.seen.is_(False)).order_by(Rating.created_at.desc()).limit(limit)
    )
    return res.scalars().all()

async def get_candidate_to_rate(session: AsyncSession, viewer: User):
    # exclude anyone you have an unseen rating toward
    subq = select(Rating.id).where(Rating.rater_tg_id==viewer.tg_id, Rating.rated_tg_id==User.tg_id, Rating.seen.is_(False)).exists()

    q_base = select(User).where(User.tg_id!=viewer.tg_id, User.blocked.is_(False), ~subq)

    # gender prefs
    # viewer.rate_pref: who viewer wants to rate
    if viewer.rate_pref in ("male","female"):
        q_base = q_base.where(User.gender==viewer.rate_pref)
    # target be_rated_by must allow viewer.gender
    if viewer.gender in ("male","female"):
        q_base = q_base.where((User.be_rated_by=="both") | (User.be_rated_by==viewer.gender))

    # prefer active in last 15 minutes
    cutoff = datetime.utcnow() - timedelta(minutes=15)
    q_active = q_base.where(User.last_active_at.is_not(None), User.last_active_at >= cutoff).order_by(User.last_active_at.desc()).limit(30)
    res = await session.execute(q_active)
    cand = res.scalars().all()
    if cand:
        return cand[0]  # most active
    # fallback: latest created
    res = await session.execute(q_base.order_by(User.created_at.desc()).limit(1))
    return res.scalars().first()

async def add_report(session: AsyncSession, reporter_tg_id: int, reported_tg_id: int, text_: str | None):
    r = Report(reporter_tg_id=reporter_tg_id, reported_tg_id=reported_tg_id, text=text_ or "", created_at=datetime.utcnow(), is_reviewed=False)
    session.add(r)
    await session.commit()
    return r

async def list_reports(session: AsyncSession, limit: int = 20):
    res = await session.execute(select(Report).where(Report.is_reviewed.is_(False)).order_by(Report.created_at.desc()).limit(limit))
    return res.scalars().all()

async def mark_report_reviewed(session: AsyncSession, report_id: int):
    await session.execute(update(Report).where(Report.id==report_id).values(is_reviewed=True))
    await session.commit()

async def ban_user(session: AsyncSession, tg_id: int):
    await session.execute(update(User).where(User.tg_id==tg_id).values(blocked=True, blocked_at=datetime.utcnow()))
    await session.commit()

async def unban_user(session: AsyncSession, tg_id: int):
    await session.execute(update(User).where(User.tg_id==tg_id).values(blocked=False, blocked_at=None))
    await session.commit()

async def get_required_channels(session: AsyncSession):
    res = await session.execute(select(RequiredChannel).where(RequiredChannel.is_active.is_(True)).order_by(RequiredChannel.id))
    return res.scalars().all()

async def list_all_channels(session: AsyncSession):
    res = await session.execute(select(RequiredChannel).order_by(RequiredChannel.id))
    return res.scalars().all()

async def add_required_channel(session: AsyncSession, username: str | None, title: str | None, link: str | None):
    c = RequiredChannel(username=username, title=title, link=link, is_active=True, created_at=datetime.utcnow())
    session.add(c)
    await session.commit()
    return c

async def toggle_channel(session: AsyncSession, channel_id: int):
    res = await session.execute(select(RequiredChannel).where(RequiredChannel.id==channel_id))
    c = res.scalar_one_or_none()
    if not c:
        return None
    c.is_active = not c.is_active
    await session.commit()
    return c
