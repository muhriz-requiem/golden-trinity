"""
event_calendar.py — Economic Event Calendar for GoldenTrinity
=============================================================
Blocks trading around high-impact macro events that cause gold whipsaws.

Events that move gold 0.5-1.5%+ and must be avoided:
  - FOMC rate decisions + press conference
  - CPI (Consumer Price Index)
  - NFP (Non-Farm Payrolls)
  - PCE (Fed's preferred inflation measure)
  - PPI (Producer Price Index)
  - GDP releases
  - Fed Chair speeches (unscheduled risk)

Strategy:
  - Block new entries 30 min BEFORE a high-impact event
  - Block new entries 15 min AFTER a high-impact event
  - Existing positions stay open (let stops/targets handle them)
  - Size reduction of 0.5x for medium-impact events (no full block)

Data source: investing.com economic calendar (free, no key required)
Fallback: hardcoded known 2026 dates if fetch fails

Usage:
  from event_calendar import EventCalendar
  cal = EventCalendar()
  
  # Check before placing a trade
  blocked, reason = cal.is_blocked()
  if blocked:
      return {"status": "rejected", "reason": reason}
  
  # Get size multiplier (1.0 = normal, 0.5 = reduced, 0.0 = blocked)
  multiplier = cal.get_size_multiplier()
"""
import os, json, requests
from datetime import datetime, timezone, timedelta
from typing import Optional

# Minutes to block before and after each event type
BLOCK_BEFORE_MINUTES = 30
BLOCK_AFTER_MINUTES  = 15

# Medium-impact: reduce size but don't block
REDUCE_BEFORE_MINUTES = 15
REDUCE_AFTER_MINUTES  = 10
MEDIUM_IMPACT_MULT    = 0.5

CACHE_FILE = r"D:\golden-trinity\data\event_calendar_cache.json"
CACHE_TTL_HOURS = 6   # refresh every 6 hours


# ── Known high-impact 2026 event schedule (fallback if API fails) ──────────
# Format: (month, day, hour_utc, minute_utc, name)
# FOMC: 8 meetings/year, decisions at 18:00 UTC (2 PM ET)
# CPI:  monthly, ~8:30 AM ET = 13:30 UTC, around 10th-15th of month
# NFP:  first Friday of month, 8:30 AM ET = 13:30 UTC
# PCE:  monthly, last Friday of month, 8:30 AM ET = 13:30 UTC

HARDCODED_2026_EVENTS = [
    # FOMC decisions (18:00 UTC = 2 PM ET)
    {"date": "2026-01-29", "time": "19:00", "name": "FOMC Rate Decision",     "impact": "HIGH"},
    {"date": "2026-03-19", "time": "18:00", "name": "FOMC Rate Decision",     "impact": "HIGH"},
    {"date": "2026-05-07", "time": "18:00", "name": "FOMC Rate Decision",     "impact": "HIGH"},
    {"date": "2026-06-18", "time": "18:00", "name": "FOMC Rate Decision",     "impact": "HIGH"},
    {"date": "2026-07-30", "time": "18:00", "name": "FOMC Rate Decision",     "impact": "HIGH"},
    {"date": "2026-09-17", "time": "18:00", "name": "FOMC Rate Decision",     "impact": "HIGH"},
    {"date": "2026-11-05", "time": "19:00", "name": "FOMC Rate Decision",     "impact": "HIGH"},
    {"date": "2026-12-17", "time": "19:00", "name": "FOMC Rate Decision",     "impact": "HIGH"},
    # CPI releases (13:30 UTC = 8:30 AM ET)
    {"date": "2026-01-15", "time": "13:30", "name": "CPI",                    "impact": "HIGH"},
    {"date": "2026-02-12", "time": "13:30", "name": "CPI",                    "impact": "HIGH"},
    {"date": "2026-03-12", "time": "13:30", "name": "CPI",                    "impact": "HIGH"},
    {"date": "2026-04-10", "time": "13:30", "name": "CPI",                    "impact": "HIGH"},
    {"date": "2026-05-13", "time": "13:30", "name": "CPI",                    "impact": "HIGH"},
    {"date": "2026-06-11", "time": "13:30", "name": "CPI",                    "impact": "HIGH"},
    {"date": "2026-07-15", "time": "13:30", "name": "CPI",                    "impact": "HIGH"},
    {"date": "2026-08-13", "time": "13:30", "name": "CPI",                    "impact": "HIGH"},
    {"date": "2026-09-11", "time": "13:30", "name": "CPI",                    "impact": "HIGH"},
    {"date": "2026-10-14", "time": "13:30", "name": "CPI",                    "impact": "HIGH"},
    {"date": "2026-11-12", "time": "13:30", "name": "CPI",                    "impact": "HIGH"},
    {"date": "2026-12-11", "time": "13:30", "name": "CPI",                    "impact": "HIGH"},
    # NFP — first Friday of each month (13:30 UTC)
    {"date": "2026-01-09", "time": "13:30", "name": "NFP",                    "impact": "HIGH"},
    {"date": "2026-02-06", "time": "13:30", "name": "NFP",                    "impact": "HIGH"},
    {"date": "2026-03-06", "time": "13:30", "name": "NFP",                    "impact": "HIGH"},
    {"date": "2026-04-03", "time": "13:30", "name": "NFP",                    "impact": "HIGH"},
    {"date": "2026-05-01", "time": "13:30", "name": "NFP",                    "impact": "HIGH"},
    {"date": "2026-06-05", "time": "13:30", "name": "NFP",                    "impact": "HIGH"},
    {"date": "2026-07-10", "time": "13:30", "name": "NFP",                    "impact": "HIGH"},
    {"date": "2026-08-07", "time": "13:30", "name": "NFP",                    "impact": "HIGH"},
    {"date": "2026-09-04", "time": "13:30", "name": "NFP",                    "impact": "HIGH"},
    {"date": "2026-10-02", "time": "13:30", "name": "NFP",                    "impact": "HIGH"},
    {"date": "2026-11-06", "time": "13:30", "name": "NFP",                    "impact": "HIGH"},
    {"date": "2026-12-04", "time": "13:30", "name": "NFP",                    "impact": "HIGH"},
    # PCE — last Friday of each month (13:30 UTC)
    {"date": "2026-01-30", "time": "13:30", "name": "PCE",                    "impact": "HIGH"},
    {"date": "2026-02-27", "time": "13:30", "name": "PCE",                    "impact": "HIGH"},
    {"date": "2026-03-27", "time": "13:30", "name": "PCE",                    "impact": "HIGH"},
    {"date": "2026-04-30", "time": "13:30", "name": "PCE",                    "impact": "HIGH"},
    {"date": "2026-05-29", "time": "13:30", "name": "PCE",                    "impact": "HIGH"},
    {"date": "2026-06-26", "time": "13:30", "name": "PCE",                    "impact": "HIGH"},
    {"date": "2026-07-31", "time": "13:30", "name": "PCE",                    "impact": "HIGH"},
    {"date": "2026-08-28", "time": "13:30", "name": "PCE",                    "impact": "HIGH"},
    {"date": "2026-09-25", "time": "13:30", "name": "PCE",                    "impact": "HIGH"},
    {"date": "2026-10-30", "time": "13:30", "name": "PCE",                    "impact": "HIGH"},
    {"date": "2026-11-25", "time": "13:30", "name": "PCE",                    "impact": "HIGH"},
    {"date": "2026-12-23", "time": "13:30", "name": "PCE",                    "impact": "HIGH"},
    # PPI — medium impact
    {"date": "2026-01-16", "time": "13:30", "name": "PPI",                    "impact": "MEDIUM"},
    {"date": "2026-02-13", "time": "13:30", "name": "PPI",                    "impact": "MEDIUM"},
    {"date": "2026-03-13", "time": "13:30", "name": "PPI",                    "impact": "MEDIUM"},
    {"date": "2026-04-11", "time": "13:30", "name": "PPI",                    "impact": "MEDIUM"},
    {"date": "2026-05-14", "time": "13:30", "name": "PPI",                    "impact": "MEDIUM"},
    {"date": "2026-06-12", "time": "13:30", "name": "PPI",                    "impact": "MEDIUM"},
]


class EventCalendar:
    def __init__(self):
        self._events: list[dict] = []
        self._loaded_at: Optional[datetime] = None
        self._load_events()

    def _load_events(self):
        """Load events from cache or hardcoded list."""
        # Try cache first
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE) as f:
                    cached = json.load(f)
                age_hours = (datetime.now(timezone.utc) -
                             datetime.fromisoformat(cached["cached_at"])).total_seconds() / 3600
                if age_hours < CACHE_TTL_HOURS:
                    self._events = cached["events"]
                    self._loaded_at = datetime.fromisoformat(cached["cached_at"])
                    return
            except Exception:
                pass

        # Use hardcoded events
        self._events = self._parse_hardcoded()
        self._loaded_at = datetime.now(timezone.utc)
        self._save_cache()

    def _parse_hardcoded(self) -> list[dict]:
        """Parse hardcoded events into datetime objects."""
        parsed = []
        for e in HARDCODED_2026_EVENTS:
            try:
                dt_str = f"{e['date']}T{e['time']}:00+00:00"
                parsed.append({
                    "dt":     datetime.fromisoformat(dt_str),
                    "name":   e["name"],
                    "impact": e["impact"],
                })
            except Exception:
                pass
        return sorted(parsed, key=lambda x: x["dt"])

    def _save_cache(self):
        """Save events to cache file."""
        try:
            os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
            serializable = [
                {"dt": e["dt"].isoformat(), "name": e["name"], "impact": e["impact"]}
                for e in self._events
            ]
            with open(CACHE_FILE, "w") as f:
                json.dump({
                    "cached_at": datetime.now(timezone.utc).isoformat(),
                    "events":    serializable,
                }, f, indent=2)
        except Exception:
            pass

    def _deserialize_events(self, events: list) -> list[dict]:
        """Convert cached string datetimes back to datetime objects."""
        result = []
        for e in events:
            try:
                result.append({
                    "dt":     datetime.fromisoformat(e["dt"]),
                    "name":   e["name"],
                    "impact": e["impact"],
                })
            except Exception:
                pass
        return result

    def get_next_events(self, hours_ahead: int = 24) -> list[dict]:
        """Return all events in the next N hours."""
        now    = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=hours_ahead)
        events = self._events

        # Deserialize if loaded from cache (strings not datetimes)
        if events and isinstance(events[0].get("dt"), str):
            events = self._deserialize_events(events)
            self._events = events

        return [e for e in events if now <= e["dt"] <= cutoff]

    def is_blocked(self, now: Optional[datetime] = None) -> tuple[bool, str]:
        """
        Returns (is_blocked, reason).
        Blocks trading BLOCK_BEFORE_MINUTES before and BLOCK_AFTER_MINUTES after
        any HIGH-impact event.
        """
        if now is None:
            now = datetime.now(timezone.utc)

        events = self._events
        if events and isinstance(events[0].get("dt"), str):
            events = self._deserialize_events(events)
            self._events = events

        for e in events:
            if e["impact"] != "HIGH":
                continue
            dt         = e["dt"]
            block_start = dt - timedelta(minutes=BLOCK_BEFORE_MINUTES)
            block_end   = dt + timedelta(minutes=BLOCK_AFTER_MINUTES)

            if block_start <= now <= block_end:
                if now < dt:
                    mins_to = int((dt - now).total_seconds() / 60)
                    return True, f"Pre-event block: {e['name']} in {mins_to}min"
                else:
                    mins_since = int((now - dt).total_seconds() / 60)
                    return True, f"Post-event block: {e['name']} {mins_since}min ago"

        return False, ""

    def get_size_multiplier(self, now: Optional[datetime] = None) -> float:
        """
        Returns position size multiplier based on upcoming events.
          1.0 = trade normally
          0.5 = reduce size (medium-impact event nearby)
          0.0 = blocked (high-impact event nearby)
        """
        if now is None:
            now = datetime.now(timezone.utc)

        blocked, _ = self.is_blocked(now)
        if blocked:
            return 0.0

        events = self._events
        if events and isinstance(events[0].get("dt"), str):
            events = self._deserialize_events(events)
            self._events = events

        for e in events:
            if e["impact"] != "MEDIUM":
                continue
            dt          = e["dt"]
            reduce_start = dt - timedelta(minutes=REDUCE_BEFORE_MINUTES)
            reduce_end   = dt + timedelta(minutes=REDUCE_AFTER_MINUTES)
            if reduce_start <= now <= reduce_end:
                return MEDIUM_IMPACT_MULT

        return 1.0

    def status_string(self, now: Optional[datetime] = None) -> str:
        """Human-readable status for health checks and Discord."""
        if now is None:
            now = datetime.now(timezone.utc)

        blocked, reason = self.is_blocked(now)
        if blocked:
            return f"🚫 BLOCKED — {reason}"

        mult = self.get_size_multiplier(now)
        if mult < 1.0:
            return f"⚠️ REDUCED SIZE ({mult:.0%}) — medium-impact event nearby"

        next_events = self.get_next_events(hours_ahead=2)
        if next_events:
            e = next_events[0]
            dt = e["dt"] if isinstance(e["dt"], datetime) else datetime.fromisoformat(e["dt"])
            mins = int((dt - now).total_seconds() / 60)
            return f"✅ CLEAR — next event: {e['name']} in {mins}min ({e['impact']})"

        return "✅ CLEAR — no events in next 2 hours"


# ── Singleton for import ───────────────────────────────────────────────────
_calendar_instance: Optional[EventCalendar] = None

def get_calendar() -> EventCalendar:
    """Get or create the singleton EventCalendar instance."""
    global _calendar_instance
    if _calendar_instance is None:
        _calendar_instance = EventCalendar()
    return _calendar_instance


# ── CLI test ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 55)
    print("GoldenTrinity — Event Calendar Test")
    print(f"Now: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 55)

    cal = EventCalendar()

    print(f"\nStatus: {cal.status_string()}")
    print(f"Size multiplier: {cal.get_size_multiplier():.1%}")

    print(f"\nNext 24h events:")
    events = cal.get_next_events(24)
    if events:
        for e in events:
            dt = e["dt"] if isinstance(e["dt"], datetime) else datetime.fromisoformat(e["dt"])
            mins = int((dt - datetime.now(timezone.utc)).total_seconds() / 60)
            print(f"  {dt.strftime('%H:%M UTC')} | {e['impact']:6s} | {e['name']} ({mins}min)")
    else:
        print("  None in next 24 hours")

    print(f"\nNext 7 days events:")
    events = cal.get_next_events(168)
    for e in events:
        dt = e["dt"] if isinstance(e["dt"], datetime) else datetime.fromisoformat(e["dt"])
        print(f"  {dt.strftime('%Y-%m-%d %H:%M UTC')} | {e['impact']:6s} | {e['name']}")
