GitHubAgent fetches recent GitHub repositories, configured release feeds, and enabled technology sources; it translates item titles and summaries locally through Ollama or Argos Translate, ranks items against optional user-maintained interests, publishes `intel.item` and `intel.batch` events, and persistently retries translations that are not yet available.

The SQLite database at `~/.cache/rasool/github_agent/queue.sqlite` contains
`translation_queue` for pending translations and `item_lifecycle` for
per-item states and retry metadata. The lifecycle table is created
automatically on startup; status counts are written to the application log.

Successfully published `intel.item` events are also persisted by the
supervisor's `IntelFeedSink` to `~/.cache/rasool/intel_feed.sqlite`. A
`published` lifecycle status means EventBus delivery succeeded; a row with
`status='persisted'` in `intel_feed` confirms local durable storage. The
sink deduplicates on URL and updates the stored translation if that URL is
published again. Persistence is local only; it does not imply delivery to an
external dashboard or notification service.
