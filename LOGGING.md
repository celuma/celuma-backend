# Logging Guide (Celuma API)

This guide explains how we log events in the API, the iconography used in messages, and best practices for adding new logs. The goal is to have useful, consistent, and safe traces.

## Principles

- Structured and consistent logs (use `extra={...}` for metadata).
- Do not expose sensitive information (tokens, cookies, passwords).
- Include `request_id` to correlate middleware and endpoints.
- Proper levels: `INFO` for normal flow, `WARNING` for expected anomalies, `ERROR/EXCEPTION` for failures.

## Request ID and Correlation

- Every request receives an `X-Request-ID` generated in middleware.
- The ID is added to `response.headers` and is available as `request.state.request_id`.
- In logs you will see a short prefix: `[abcd1234]` (first 8 characters) for readability.

Endpoint example:

```python
request_id = getattr(request.state, "request_id", "unknown")[:8]
logger.info("Login attempt", extra={"event": "auth.login.attempt", "request_id": request_id})
```

## Iconography and Common Messages

- 🔥 INCOMING REQUEST: start of an HTTP request; includes method, URL, and client.
- 📋 Headers: request headers. Sensitive values are redacted as `REDACTED`.
- ✅ RESPONSE: successful response (<400). Includes `status_code` and duration.
- ⚠️ RESPONSE: warning response (4xx).
- ❌ RESPONSE: server error (5xx).
- 💥 Unhandled exception: unhandled exception; prints traceback with `logger.exception`.
- 🔐 Authenticating token: start of JWT authentication.
- 🚫 Token is blacklisted: revoked token.
- 🎉 Authentication successful: successful authentication.
- 🔍 GET /auth/me: access to the current profile.
- 👤 User details: non-sensitive user details (email, username, role).
- 📝 Update data received: profile update data (no passwords).

Note: For responses and headers, we log headers only for `auth` endpoints or on errors to reduce noise.

## Structured Events (`extra`)

We use the `event` key to identify the event type and stable fields to facilitate search/alerting:

- Authentication:
  - `auth.login.attempt` { username_or_email, tenant_id? }
  - `auth.login.success` { user_id, tenant_id }
  - `auth.login.invalid_credentials` { username_or_email, tenant_id? }
  - `auth.login.need_tenant_selection` { options_count }
  - `auth.register` / `auth.register.success` / email/username conflicts
  - `auth.logout.request` / `auth.logout.success` / `auth.logout.already_revoked`
  - `auth.register_unified.*` for unified registration (tenant/branch/user)

- HTTP Middleware:
  - Entry/exit logs with `request_id`, method, URL, `client_ip`, `status_code`, `process_time`.

Example:

```python
logger.info(
    "Login success",
    extra={"event": "auth.login.success", "user_id": str(user.id), "tenant_id": str(user.tenant_id)}
)
```

## Header Redaction and PII

- Automatically redacted: `Authorization`, `Cookie`, `Set-Cookie`, `X-API-Key`.
- Do not log passwords, hashes, full tokens, or full request/response bodies.
- For tokens, when useful, log a safe prefix: `token[:20] + "..."`.

## Relevant Middlewares

1) Request ID and Security Headers
- Injects `X-Request-ID` and adds headers: `X-Content-Type-Options`, `X-Frame-Options`, `X-XSS-Protection`, `Referrer-Policy`, `Permissions-Policy`.

2) Request Size Limiting
- Rejects bodies >10MB with `413`.

3) Basic Rate Limiting (in-memory)
- 100 req/min per IP, returns `429` when exceeded.
- Use Redis or another distributed store in production.

4) HTTP Logging
- Entry/exit, header redaction, status code-based symbols, `logger.exception` on errors.

5) Global Exception Handler
- Returns `500` with `request_id`. Error details are logged with traceback.

## Levels and Recommended Usage

- `logger.info`: normal flow (start/end, expected decisions, results).
- `logger.warning`: anomalous but expected conditions (invalid credentials, uniqueness conflicts).
- `logger.error` / `logger.exception`: server errors, unexpected failures.
  - Use `logger.exception` inside `except` to include traceback.

## Best Practices When Adding Logs

- Use clear, concise messages and add context in `extra`.
- Include stable identifiers: `event`, `request_id`, `user_id`, `tenant_id`.
- Avoid logging large payloads; do not duplicate heavy information.
- Do not expose secrets or sensitive data.
- Keep consistency with existing events.

## How to View Logs

```bash
# Local logs (make)
make logs

# Docker container logs
docker logs celuma-backend-api-1
```

---

If you see opportunities to improve the log structure or add new events, open a PR with examples and justification.
