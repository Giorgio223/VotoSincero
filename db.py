import os
from datetime import datetime, timedelta

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Integer, String, Text,
    ForeignKey, select, update, func, text
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, aliased
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.postgresql import insert as pg_insert


# ---------- Helpers ----------
def _nonempty_text(col):
    """SQL expression: column is NOT NULL and not empty/whitespace.

    Some deployments may contain legacy/dirty rows (e.g., from older bot
    versions) where required fields are NULL or empty. For matching in
    "Valuta" we only want fully filled анкеты.
    """
    return col.is_not(None) & (func.length(func.trim(col)) > 0)


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


def profile_complete_filter():
    """SQLAlchemy boolean expression: keep only fully completed анкеты.

    We don't rely solely on model-level `nullable=False` because in real DBs
    there can be legacy rows with NULLs/empty strings (e.g., after older
    versions, manual imports, or partial writes).

    This filter is used in "Valuta" so unfinished анкеты never appear.
    """

    return (
        _nonempty_text(User.name)
        & User.age.is_not(None)
        & (User.age >= 9)
        & (User.age <= 100)
        & _nonempty_text(User.city)
        & _nonempty_text(User.photo_file_id)
        & User.gender.in_(["male", "female"])
        & User.rate_pref.in_(["male", "female", "both"])
        & User.be_rated_by.in_(["male", "female", "both"])
    )


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
    return create_async_engine(db_url, echo=True)


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

            # В некоторых ранних деплоях мог быть создан другой UNIQUE,
            # который запрещает повторную оценку, пока предыдущая не просмотрена,
            # но без учёта версии фото. Это ломает нашу логику (разрешаем оценку
            # новой версии фото независимо от старой).
            await conn.execute(text("ALTER TABLE ratings DROP CONSTRAINT IF EXISTS uq_rater_rated_unseen"))
            await conn.execute(text("DROP INDEX IF EXISTS uq_rater_rated_unseen"))

            # Вместо этого держим уникальность только для 'непросмотренной' оценки
            # В РАМКАХ ОДНОЙ ВЕРСИИ ФОТО. Это защищает от дабл-кликов/повторов.
            await conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_rater_rated_unseen_idx "
                "ON ratings (rater_tg_id, rated_tg_id, rated_photo_version) "
                "WHERE seen = FALSE"
            ))


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


async def list_all_user_tg_ids(session: AsyncSession, include_blocked: bool = False) -> list[int]:
    """Return Telegram IDs of all known users.

    NOTE: In this project a "user" is created after registration, so this
    list corresponds to everyone who has completed registration at least once.
    """
    q = select(User.tg_id)
    if not include_blocked:
        q = q.where(User.blocked.is_(False))
    res = await session.execute(q.order_by(User.id))
    return [int(x) for (x,) in res.all()]


# ---------- Ratings ----------
async def save_rating(session: AsyncSession, rater_tg_id: int, rated_user: User, score: int, message: str | None):
    """Persist a rating.

    В проде часто встречается двойной клик по кнопке (Telegram повторяет update)
    или гонка, когда одна и та же оценка пытается записаться дважды.

    Также в старых БД мог оставаться UNIQUE на unseen-оценку без учёта версии фото.
    Мы его удаляем в init_db, но на всякий случай делаем запись идемпотентной:
    если unseen-оценка на эту же версию фото уже есть — обновляем её.
    """

    now = datetime.utcnow()

    # Оптимальный путь для PostgreSQL: UPSERT по нашему парциальному индексу.
    if session.bind and session.bind.dialect.name == "postgresql":
        t = Rating.__table__
        stmt = (
            pg_insert(t)
            .values(
                rater_tg_id=rater_tg_id,
                rated_tg_id=rated_user.tg_id,
                score=score,
                message=message,
                rated_photo_version=rated_user.photo_version,
                seen=False,
                created_at=now,
            )
            .on_conflict_do_update(
                index_elements=[t.c.rater_tg_id, t.c.rated_tg_id, t.c.rated_photo_version],
                index_where=(t.c.seen.is_(False)),
                set_={
                    "score": score,
                    "message": message,
                    "created_at": now,
                },
            )
            .returning(t.c.id)
        )

        res = await session.execute(stmt)
        rid = int(res.scalar_one())
        r = await session.get(Rating, rid)
    else:
        # SQLite / fallback: обычная вставка + обработка ошибки
        r = Rating(
            rater_tg_id=rater_tg_id,
            rated_tg_id=rated_user.tg_id,
            score=score,
            message=message,
            rated_photo_version=rated_user.photo_version,
            seen=False,
            created_at=now,
        )
        session.add(r)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            # Если уже есть unseen запись — обновим её
            await session.execute(
                update(Rating)
                .where(
                    Rating.rater_tg_id == rater_tg_id,
                    Rating.rated_tg_id == rated_user.tg_id,
                    Rating.rated_photo_version == rated_user.photo_version,
                    Rating.seen.is_(False),
                )
                .values(score=score, message=message, created_at=now)
            )

    # отметим активность того, кто ставит оценку
    await session.execute(
        update(User)
        .where(User.tg_id == rater_tg_id)
        .values(last_active_at=now)
    )
    await session.commit()
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
    2) Если таких нет — случайно как раньше.

    Анти-дубль (НОВАЯ ЛОГИКА):
    - Не показываем кандидата снова, если viewer уже оценил его текущую фотку
      и этот голос ещё НЕ просмотрен кандидатом (seen=False).
    - Как только кандидат посмотрел в "Chi mi ha valutato" (seen=True) —
      viewer может снова получить этого кандидата в Valuta.
    """

    gender_filter = None if viewer.rate_pref == "both" else viewer.rate_pref
    allowed_rater_gender = viewer.gender  # male/female

    now = datetime.utcnow()
    active_since = now - timedelta(minutes=15)

    def build_query(active_only: bool):
        q = (
            select(User)
            .where(
                User.tg_id != viewer.tg_id,
                User.blocked.is_(False),
                profile_complete_filter(),
            )
            .order_by(func.random())
            .limit(1)
        )

        if active_only:
            q = q.where(User.last_active_at.is_not(None), User.last_active_at >= active_since)

        if gender_filter:
            q = q.where(User.gender == gender_filter)

        q = q.where((User.be_rated_by == "both") | (User.be_rated_by == allowed_rater_gender))

        subq_unseen = (
            select(Rating.id)
            .where(
                Rating.rater_tg_id == viewer.tg_id,
                Rating.rated_tg_id == User.tg_id,
                Rating.rated_photo_version == User.photo_version,
                Rating.seen.is_(False),
            )
            .correlate(User)
            .exists()
        )
        q = q.where(~subq_unseen)
        return q

    res = await session.execute(build_query(active_only=True))
    cand = res.scalar_one_or_none()
    if cand:
        return cand

    res = await session.execute(build_query(active_only=False))
    return res.scalar_one_or_none()


# ---------- Leaderboard ----------
async def get_top3(session: AsyncSession):
    # We want the leaderboard to account for both the average score and
    # the number of votes. A high average with only 1 vote should not beat
    # a slightly lower average with hundreds of votes.
    #
    # Use a Bayesian (IMDB-style) weighted rating:
    #   WR = (v/(v+m))*R + (m/(v+m))*C
    # where:
    #   R = user's average, v = user's votes count,
    #   C = global average, m = минимальный "вес" (smoothing).
    m = 20  # tune if needed

    overall_avg_subq = (
        select(func.avg(Rating.score))
        .join(User, Rating.rated_tg_id == User.tg_id)
        .where(
            Rating.rated_photo_version == User.photo_version,
            User.blocked.is_(False),
            profile_complete_filter(),
        )
    ).scalar_subquery()

    avg_score = func.avg(Rating.score)
    cnt = func.count(Rating.id)
    weighted = (cnt / (cnt + m)) * avg_score + (m / (cnt + m)) * overall_avg_subq

    q = (
        select(User, avg_score.label("avg_score"), cnt.label("cnt"))
        .join(Rating, Rating.rated_tg_id == User.tg_id)
        .where(
            Rating.rated_photo_version == User.photo_version,
            User.blocked.is_(False),
            profile_complete_filter(),
        )
        .group_by(User.tg_id, User.id)
        .having(cnt > 0)
        .order_by(weighted.desc(), cnt.desc(), avg_score.desc())
        .limit(3)
    )
    res = await session.execute(q)
    return res.all()


async def get_my_rank(session: AsyncSession, me: User):
    m = 20

    overall_avg_subq = (
        select(func.avg(Rating.score))
        .join(User, Rating.rated_tg_id == User.tg_id)
        .where(
            Rating.rated_photo_version == User.photo_version,
            User.blocked.is_(False),
            profile_complete_filter(),
        )
    ).scalar_subquery()

    avg_score = func.avg(Rating.score)
    cnt = func.count(Rating.id)
    weighted = (cnt / (cnt + m)) * avg_score + (m / (cnt + m)) * overall_avg_subq

    q = (
        select(User.tg_id, avg_score.label("avg_score"), cnt.label("cnt"))
        .join(Rating, Rating.rated_tg_id == User.tg_id)
        .where(
            Rating.rated_photo_version == User.photo_version,
            User.blocked.is_(False),
            profile_complete_filter(),
        )
        .group_by(User.tg_id)
        .having(cnt > 0)
        .order_by(weighted.desc(), cnt.desc(), avg_score.desc())
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
