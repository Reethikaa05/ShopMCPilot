# Limitations & the right long-term fix

## Limitations (honest list)
1. **Polling, not push.** No webhooks, so dashboards/alerts lag by the cache TTL (15s default).
2. **Pull-time analytics.** Forecast/health re-scan orders per call (<= 3k orders). Fine for SMB stores, wasteful at 100k+ orders/month.
3. **wc-auth needs a public HTTPS callback** for real stores (WooCommerce refuses http callbacks). Use a tunnel (ngrok/cloudflared) or paste a read-only key. This is a WooCommerce constraint, not a connector one.
4. **HTTP basic over HTTPS only.** Plain-http stores need OAuth 1.0a signing: intentionally unsupported; the client refuses non-localhost http.
5. **Single tenant per process.** One credential set per server instance; the cache is in-memory and per-process.
6. **Secrets at rest** = 0600 file or env vars. Fine for a developer laptop/container, not for a multi-tenant SaaS.
7. **Rate limits are inferred.** WooCommerce itself publishes none; the limiter reacts to 429/5xx from hosts/WAFs and cannot see a host's CPU pressure.
8. **Mock != real store.** The mock reproduces headers, pagination and error shapes but not plugin-specific behaviour (HPOS, custom statuses, WPML...).

## Long-term fix (production architecture)
* **Webhook ingest + local read model.** Register `order.created/updated`, `product.updated` webhooks (HMAC-verified) into a queue -> Postgres. Agent tools then read the *replica*: millisecond latency, zero load on the merchant, analytics become SQL, and rate limits stop mattering. Nightly reconciliation job heals missed webhooks.
* **Platform-managed OAuth + vault.** Agent Studio stores per-merchant tokens in KMS/Vault, rotates keys, supports revocation, and the connector becomes stateless/multi-tenant.
* **Policy layer.** Per-merchant PII policy, per-tool scopes and approval workflows before introducing any write tools (refund/cancel) with idempotency keys and human-in-the-loop.
* **Observability.** Export the audit stream as OpenTelemetry spans; alert on throttle ratio, auth failures, and unusual tool-call volume.
