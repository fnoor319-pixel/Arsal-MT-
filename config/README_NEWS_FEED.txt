SPARTAN PRO NEWS CALENDAR ADAPTER
=================================

The bot reads config/news_events.json. A real calendar bridge/provider should
rewrite this file atomically with UTC ISO timestamps.

Schema example:
{
  "generated_at": "2026-08-27T12:00:00Z",
  "provider": "your-provider-name",
  "events": [
    {
      "time_utc": "2026-08-27T12:30:00Z",
      "name": "US GDP",
      "currency": "USD",
      "impact": "high",
      "category": "macro"
    },
    {
      "time_utc": "2026-08-27T14:30:00Z",
      "name": "EIA Crude Oil Inventories",
      "currency": "USD",
      "impact": "high",
      "category": "EIA"
    }
  ]
}

Default SPARTAN_NEWS_HARD_GATE=False so an unconfigured feed does not stop the
existing working DEMO bot. When a reliable live provider is connected, set it
True in settings.py so missing/stale news becomes HOLD.
