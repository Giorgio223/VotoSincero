import os
from datetime import datetime, timedelta

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Integer, String, Text,
    ForeignKey, select, update, func, text
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, aliased
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.exc import IntegrityError


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)

    name: Mapped[str] = mapped_column(Text, nullable=False)
    age: Mapped[int] = mapped_column(Integer, nullable=False)
    city: Mapped[str] = mapped_column(Text, nullable=False)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)

    photo_file_id: Mapped[str] = mapped_column(Text, nullable=False)

    gender: Mapped[str] = mapped_column(String(10), nullable=False)       # male/female
    rate_pref: Mapped[str] = mapped_column(String(10), nullable=False)    # male/female/both
    be_rated_by: Mapped[str] = mapped_column(String(10), nullable=False, server_default="both")  # male/female/both

    photo_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

    blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Обновляется, когда юзер ставит оценку (нужно для приоритета выдачи кандидатов)
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class Rating(Base):
    __tablename__ = "ratings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rater_tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    rated_tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    score: Mapped[int] = mapped_column(Integer, nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    rated_photo_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    seen: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    # ВАЖНО: мы намеренно НЕ держим UNIQUE на (rater, rated, photo_version)
    # потому что по новой логике повторная оценка разрешается после того,
    # как rated реально посмотрел предыдущую оценку (seen=True).


class RequiredChannel(Base):
    __tablename__ = "required_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)  # @channel or -100...
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    link: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reporter_tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reported_tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reported_photo_version: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="open")  # open/closed


def create_engine(db_url: str):
    return create_async_engine(db_url, echo=False)


def create_sessionmaker(engine):
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # Миграции делаем только для PostgreSQL.
        # SQLite (локально на Windows) не поддерживает DO $$ и IF NOT EXISTS в ALTER TABLE в том же виде.
        if conn.dialect.name == "postgresql":
            # На случай если ты уже создавал таблицы раньше — добавим поля безопасно
            await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS be_rated_by VARCHAR(10) DEFAULT 'both'"))
            await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS photo_version INTEGER DEFAULT 1"))
            await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS blocked BOOLEAN DEFAULT FALSE"))
            await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS blocked_at TIMESTAMP NULL"))
            await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active_at TIMESTAMP NULL"))

            await conn.execute(text("ALTER TABLE ratings ADD COLUMN IF NOT EXISTS rated_photo_version INTEGER DEFAULT 1"))
            await conn.execute(text("ALTER TABLE ratings ADD COLUMN IF NOT EXISTS seen BOOLEAN DEFAULT FALSE"))

            # Старый UNIQUE-констрейнт мешает повторной оценке.
            # Если он уже был создан раньше — удалим.
            await conn.execute(text("ALTER TABLE ratings DROP CONSTRAINT IF EXISTS uq_rater_rated_version"))
            await conn.execute(text("ALTER TABLE ratings DROP CONSTRAINT IF EXISTS uq_rater_rated"))
            await conn.execute(text("DROP INDEX IF EXISTS uq_rater_rated"))
            await conn.execute(text("DROP INDEX IF EXISTS uq_rater_rated_version"))
            # Уникальность только на НЕПРОСМОТРЕННЫЕ оценки (чтобы можно было оценить снова после seen=True)
            await conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_rater_rated_unseen ON ratings (rater_tg_id, rated_tg_id) WHERE seen IS false"))
            # Индексы для скорости на больших объёмах
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_users_last_active_at ON users(last_active_at DESC)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ratings_rated_seen ON ratings(rated_tg_id, rated_photo_version, seen)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ratings_rater_seen ON ratings(rater_tg_id, seen)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_reports_status_created ON reports(status, created_at DESC)"))


# ---------- Users ----------
async def get_user_by_tg_id(session: AsyncSession, tg_id: int) -> User | None:
    res = await session.execute(select(User).where(User.tg_id == tg_id))
    return res.scalar_one_or_none()


async def create_user(
    session: AsyncSession,
    tg_id: int,
    name: str,
    age: int,
    city: str,
    bio: str | None,
    photo_file_id: str,
    gender: str,
    rate_pref: str,
    be_rated_by: str,
    photo_version: int = 1,
):
    user = User(
        tg_id=tg_id,
        name=name,
        age=age,
        city=city,
        bio=bio,
        photo_file_id=photo_file_id,
        gender=gender,
        rate_pref=rate_pref,
        be_rated_by=be_rated_by,
        photo_version=photo_version,
    )
    session.add(user)
    await session.commit()
    return user


async def update_user_fields(session: AsyncSession, tg_id: int, **fields):
    await session.execute(update(User).where(User.tg_id == tg_id).values(**fields))
    await session.commit()


async def bump_photo_version_and_update_photo(session: AsyncSession, tg_id: int, new_photo_id: str):
    user = await get_user_by_tg_id(session, tg_id)
    if not user:
        return
    new_ver = int(user.photo_version) + 1
    await session.execute(
        update(User)
        .where(User.tg_id == tg_id)
        .values(photo_file_id=new_photo_id, photo_version=new_ver)
    )
    await session.commit()


async def block_user(session: AsyncSession, tg_id: int):
    await session.execute(
        update(User)
        .where(User.tg_id == tg_id)
        .values(blocked=True, blocked_at=datetime.utcnow())
    )
    await session.commit()


async def unblock_user(session: AsyncSession, tg_id: int):
    await session.execute(
        update(User)
        .where(User.tg_id == tg_id)
        .values(blocked=False, blocked_at=None)
    )
    await session.commit()


# ---------- Ratings ----------
async def save_rating(session: AsyncSession, rater_tg_id: int, rated_user: User, score: int, message: str | None):
    r = Rating(
        rater_tg_id=rater_tg_id,
        rated_tg_id=rated_user.tg_id,
        score=score,
        message=message,
        rated_photo_version=rated_user.photo_version,
        seen=False,
    )
    session.add(r)
    # отметим активность того, кто ставит оценку
    await session.execute(
        update(User)
        .where(User.tg_id == rater_tg_id)
        .values(last_active_at=datetime.utcnow())
    )
    try:
        await session.commit()
    except IntegrityError:
        # Обычно означает: уже есть НЕПРОСМОТРЕННАЯ оценка viewer->candidate (uq_rater_rated_unseen)
        await session.rollback()
        return None
    return r


async def get_unseen_count(session: AsyncSession, me: User) -> int:
    res = await session.execute(
        select(func.count(Rating.id))
        .where(
            Rating.rated_tg_id == me.tg_id,
            Rating.rated_photo_version == me.photo_version,
            Rating.seen.is_(False),
        )
    )
    return int(res.scalar() or 0)


async def get_ratings_for_me_and_mark_seen(session: AsyncSession, me: User, limit: int = 5):
    q = (
        select(Rating, User)
        .join(User, User.tg_id == Rating.rater_tg_id, isouter=True)
        .where(
            Rating.rated_tg_id == me.tg_id,
            Rating.rated_photo_version == me.photo_version,
            Rating.seen.is_(False),
        )
        .order_by(Rating.created_at.desc())
        .limit(limit)
    )
    res = await session.execute(q)
    items = res.all()

    # пометим просмотренными
    ids = [r.id for r, _ in items]
    if ids:
        await session.execute(update(Rating).where(Rating.id.in_(ids)).values(seen=True))
        await session.commit()

    return items


async def get_my_rating_stats(session: AsyncSession, me: User):
    res = await session.execute(
        select(func.avg(Rating.score), func.count(Rating.id))
        .where(Rating.rated_tg_id == me.tg_id, Rating.rated_photo_version == me.photo_version)
    )
    avg, cnt = res.one()
    if cnt is None or int(cnt) == 0:
        return None, 0
    return float(avg), int(cnt)


# ---------- Candidate подбор ----------
async def get_next_candidate(session: AsyncSession, viewer: User):
    """Подбор анкеты для Valuta.

    Приоритеты:
    1) Сначала показываем тех, кто был активен последние 15 минут (ставил оценку).
    2) Если таких нет — выдача по всему пулу.

    Анти-дубль (НОВАЯ ЛОГИКА):
    - Не показываем кандидата снова, если viewer уже оценил его и этот голос ещё НЕ просмотрен кандидатом (seen=False).
    - Как только кандидат посмотрел в "Chi mi ha valutato" (seen=True) — viewer может снова получить этого кандидата.

    Оптимизация для больших объёмов:
    - избегаем ORDER BY random() (дорого на миллионах строк)
    - используем "random id pivot": ищем ближайшую анкету с id >= pivot, если нет — с id < pivot.
    """
    gender_filter = None if viewer.rate_pref == "both" else viewer.rate_pref
    allowed_rater_gender = viewer.gender  # male/female

    now = datetime.utcnow()
    active_since = now - timedelta(minutes=15)

    # Базовые условия
    def base_conditions(active_only: bool):
        conds = [
            User.tg_id != viewer.tg_id,
            User.blocked.is_(False),
            (User.be_rated_by == "both") | (User.be_rated_by == allowed_rater_gender),
        ]
        if gender_filter:
            conds.append(User.gender == gender_filter)
        if active_only:
            conds.append(User.last_active_at.is_not(None))
            conds.append(User.last_active_at >= active_since)

        # Не показываем, если есть НЕПРОСМОТРЕННАЯ оценка viewer->candidate
        subq_unseen = (
            select(Rating.id)
            .where(
                Rating.rater_tg_id == viewer.tg_id,
                Rating.rated_tg_id == User.tg_id,
                Rating.seen.is_(False),
            )
            .correlate(User)
            .exists()
        )
        conds.append(~subq_unseen)
        return conds

    async def pick_one(active_only: bool):
        # Получим min/max id по текущему фильтру, чтобы выбрать pivot.
        mm = await session.execute(select(func.min(User.id), func.max(User.id)).where(*base_conditions(active_only)))
        min_id, max_id = mm.one()
        if not min_id or not max_id:
            return None
        pivot = int(min_id + (max_id - min_id) * (os.urandom(2)[0] / 255.0))  # быстрый псевдорандом

        # 1) пробуем справа
        q1 = select(User).where(*base_conditions(active_only), User.id >= pivot).order_by(User.id).limit(1)
        res1 = await session.execute(q1)
        u = res1.scalar_one_or_none()
        if u:
            return u

        # 2) пробуем слева
        q2 = select(User).where(*base_conditions(active_only), User.id < pivot).order_by(User.id.desc()).limit(1)
        res2 = await session.execute(q2)
        return res2.scalar_one_or_none()

    cand = await pick_one(active_only=True)
    if cand:
        return cand
    return await pick_one(active_only=False)


# ---------- Leaderboard ----------
async def get_top3(session: AsyncSession):
    q = (
        select(User, func.avg(Rating.score).label("avg_score"), func.count(Rating.id).label("cnt"))
        .join(Rating, Rating.rated_tg_id == User.tg_id)
        .where(
            Rating.rated_photo_version == User.photo_version,
            User.blocked.is_(False),
        )
        .group_by(User.tg_id, User.id)
        .having(func.count(Rating.id) > 0)
        .order_by(func.avg(Rating.score).desc(), func.count(Rating.id).desc())
        .limit(3)
    )
    res = await session.execute(q)
    return res.all()


async def get_my_rank(session: AsyncSession, me: User):
    q = (
        select(User.tg_id, func.avg(Rating.score).label("avg_score"), func.count(Rating.id).label("cnt"))
        .join(Rating, Rating.rated_tg_id == User.tg_id)
        .where(
            Rating.rated_photo_version == User.photo_version,
            User.blocked.is_(False),
        )
        .group_by(User.tg_id)
        .having(func.count(Rating.id) > 0)
        .order_by(func.avg(Rating.score).desc(), func.count(Rating.id).desc())
    )
    res = await session.execute(q)
    rows = res.all()
    for idx, (tg_id, _, __) in enumerate(rows, start=1):
        if tg_id == me.tg_id:
            return idx
    return None


# ---------- Required channels ----------
async def add_required_channel(session: AsyncSession, username: str, title: str | None, link: str | None):
    ch = RequiredChannel(username=username, title=title, link=link, is_active=True)
    session.add(ch)
    await session.commit()
    return ch


async def remove_required_channel(session: AsyncSession, username: str):
    res = await session.execute(select(RequiredChannel).where(RequiredChannel.username == username))
    ch = res.scalar_one_or_none()
    if not ch:
        return False
    await session.delete(ch)
    await session.commit()
    return True


async def list_required_channels(session: AsyncSession):
    res = await session.execute(select(RequiredChannel).where(RequiredChannel.is_active.is_(True)).order_by(RequiredChannel.id))
    return res.scalars().all()


# ---------- Reports ----------
async def create_report(session: AsyncSession, reporter_tg_id: int, reported_user: User):
    rep = Report(
        reporter_tg_id=reporter_tg_id,
        reported_tg_id=reported_user.tg_id,
        reported_photo_version=reported_user.photo_version,
        status="open",
    )
    session.add(rep)
    await session.commit()
    return rep


async def get_report(session: AsyncSession, report_id: int) -> Report | None:
    res = await session.execute(select(Report).where(Report.id == report_id))
    return res.scalar_one_or_none()


async def list_open_reports(session: AsyncSession, limit: int = 20, offset: int = 0):
    """Список открытых жалоб для админки.

    Returns: list[(Report, reported_user, reporter_user_or_None)]
    """
    Reporter = aliased(User)
    q = (
        select(Report, User, Reporter)
        .join(User, User.tg_id == Report.reported_tg_id)
        .join(Reporter, Reporter.tg_id == Report.reporter_tg_id, isouter=True)
        .where(Report.status == "open")
        .order_by(Report.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    res = await session.execute(q)
    items = []
    for rep, reported, reporter in res.all():
        items.append((rep, reported, reporter))
    return items


async def close_report(session: AsyncSession, report_id: int):
    await session.execute(update(Report).where(Report.id == report_id).values(status="closed"))
    await session.commit()
