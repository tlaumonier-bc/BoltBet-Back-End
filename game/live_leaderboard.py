import os
from django.contrib.auth import get_user_model
from .models import RoundResult, PlayerStats

User = get_user_model()
BACKEND = os.environ.get("LIVE_LEADERBOARD_BACKEND", "postgres").lower()
LIMIT = int(os.environ.get("LEADERBOARD_LIMIT", "20"))


class PostgresLiveBoard:
    backend = "postgres"

    def incr(self, rnd, user_id, delta):
        # No-op: RoundResult.points is the source of truth and is updated
        # transactionally in services._finalize_pick.
        return

    def top(self, rnd, limit=LIMIT):
        if rnd is None:
            return []
        rows = (RoundResult.objects.filter(round=rnd).select_related("user")
                .order_by("-points")[:limit])
        meta = {s.user_id: s.country for s in
                PlayerStats.objects.filter(user_id__in=[r.user_id for r in rows])}
        return [{"username": r.user.username, "country": meta.get(r.user_id, "XX"),
                 "points": r.points} for r in rows]

    def reset(self, rnd):
        return


class RedisLiveBoard:
    backend = "redis"

    def __init__(self):
        import redis
        self.r = redis.Redis(
            host=os.environ.get("REDIS_HOST", "localhost"),
            port=int(os.environ.get("REDIS_PORT", "6379")),
            decode_responses=True,
        )
        self.ttl = int(os.environ.get("LIVE_LEADERBOARD_TTL", "300"))

    def _key(self, rnd):
        return f"lb:round:{rnd.number}"

    def incr(self, rnd, user_id, delta):
        if not delta:
            return
        k = self._key(rnd)
        self.r.zincrby(k, delta, user_id)
        self.r.expire(k, self.ttl)

    def top(self, rnd, limit=LIMIT):
        if rnd is None:
            return []
        pairs = self.r.zrevrange(self._key(rnd), 0, limit - 1, withscores=True)
        if not pairs:
            return []
        ids = [int(uid) for uid, _ in pairs]
        users = {u.id: u.username for u in User.objects.filter(id__in=ids)}
        meta = {s.user_id: s.country for s in PlayerStats.objects.filter(user_id__in=ids)}
        return [{"username": users.get(int(uid), "?"), "country": meta.get(int(uid), "XX"),
                 "points": int(score)} for uid, score in pairs]

    def reset(self, rnd):
        self.r.delete(self._key(rnd))


_board = None
def get_live_board():
    global _board
    if _board is None:
        _board = RedisLiveBoard() if BACKEND == "redis" else PostgresLiveBoard()
    return _board
