RouterAgent signs in to the configured HG8245W5 router, periodically reads its connected-client table, publishes `router.client` and first-seen `router.unknown` events, tracks router session state, and persists observed MAC addresses with first- and last-seen timestamps.

The router password is read from `~/.config/rasool/router_agent/password`.
The file must be owned by the current user and inaccessible to group/other
users (for example, mode `0600`). Inline passwords in `config.toml` are rejected.
