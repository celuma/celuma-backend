"""Contract checks of scripts/verify_billing_flow.sh after #8 (static: the script is NEVER run here,
because it creates an order, invoice and payment; it was validated by hand on a synthetic stack)."""
import pathlib
import re
import subprocess

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "verify_billing_flow.sh"
TEXT = SCRIPT.read_text(encoding="utf-8")


def test_script_has_valid_shell_syntax():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_no_credential_is_embedded_and_a_missing_token_is_a_clear_error():
    assert not re.search(r"eyJ[A-Za-z0-9_-]{10,}", TEXT)                    # no JWT literal
    assert not re.search(r"Bearer\s+[A-Za-z0-9._-]{20,}", TEXT)             # no bearer literal
    assert 'TOKEN="${1:-${TOKEN:-}}"' in TEXT and 'if [ -z "$TOKEN" ]' in TEXT and "exit 2" in TEXT
    result = subprocess.run(["bash", str(SCRIPT)], env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True)
    assert result.returncode == 2 and "falta el token" in result.stderr


def test_samples_use_an_explicit_active_catalog_type_never_the_retired_enum():
    assert '"type": "OTRO"' not in TEXT and "OTRO" not in TEXT
    assert "sample_type_id" in TEXT and "/api/v1/sample-types/?active_only=true" in TEXT
    assert "catálogo de tipos de muestra está vacío" in TEXT


def test_one_idempotency_key_per_logical_request_reused_by_every_retry():
    assert TEXT.count("IDEMPOTENCY_KEY=") == 1                               # generated once...
    assert TEXT.count("Idempotency-Key: $IDEMPOTENCY_KEY") == 1              # ...sent by the single POST inside the loop
    assert "Idempotency-Key: $(uuidgen)" not in TEXT
    assert TEXT.index("IDEMPOTENCY_KEY=") < TEXT.index("for ATTEMPT in")      # outside the retry loop


def test_the_order_post_is_one_guarded_bounded_attempt_per_iteration():
    loop = TEXT[TEXT.index("for ATTEMPT in"):TEXT.index("ORDER_ID=$(echo")]
    assert "for ATTEMPT in 1 2 3" in loop
    assert loop.count("orders/unified") == 1                                  # a single POST per iteration, no diagnostic re-POST
    assert "set +e" in loop and "set -e" in loop                              # failures handled, never a bare assignment under set -e
    assert "--connect-timeout" in loop and "--max-time" in loop
    assert "-f " not in loop and "exit 3" in loop


# --- behaviour with a controlled curl: no network, no server -------------------------------------

FAKE_CURL = r"""#!/bin/bash
# Controlled curl for the retry tests. Serves canned JSON for the read calls; for the order POST it
# records the attempt (key + payload hash) and fails the first $FAIL_FIRST attempts as a transport error.
LOG="$FAKE_DIR/posts.log"; COUNT="$FAKE_DIR/count"
ARGS=("$@"); URL=""; METHOD="GET"; KEY=""; DATA=""
for ((i=0;i<${#ARGS[@]};i++)); do
  case "${ARGS[$i]}" in
    -X) METHOD="${ARGS[$((i+1))]}";;
    -H) case "${ARGS[$((i+1))]}" in Idempotency-Key:*) KEY="${ARGS[$((i+1))]#Idempotency-Key: }";; esac;;
    -d) DATA="${ARGS[$((i+1))]}";;
    http*) URL="${ARGS[$i]}";;
  esac
done
case "$URL" in
  */study-types/*) echo '{"study_types":[{"id":"st1","code":"HB","name":"Hist","is_active":true}]}';;
  */price-catalog/*) echo '{"prices":[{"study_type_id":"st1","unit_price":150.0}]}';;
  */patients/*) echo '[{"id":"p1","tenant_id":"t1","branch_id":"b1","first_name":"A","last_name":"B"}]';;
  */sample-types/*) echo '{"sample_types":['"$SAMPLE_TYPES_JSON"']}';;
  */laboratory/orders/unified)
    N=$(( $(cat "$COUNT" 2>/dev/null || echo 0) + 1 )); echo "$N" > "$COUNT"
    echo "$N key=$KEY payload=$(printf '%s' "$DATA" | shasum | cut -c1-12) timeout=$([[ " $* " == *" --max-time "* ]] && echo yes || echo no)" >> "$LOG"
    if [ "$N" -le "${FAIL_FIRST:-0}" ]; then exit 28; fi
    printf '%s
%s' "${ORDER_BODY}" "${ORDER_HTTP:-200}";;
  *) echo '{}';;
esac
"""


def run_script(tmp_path, fail_first, order_http="200", order_body='{"order":{"id":"o1","order_code":"HB-1"}}', samples='{"id":"s1","code":"BX"}'):
    import os

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "curl"
    fake.write_text(FAKE_CURL)
    fake.chmod(0o755)
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "FAKE_DIR": str(tmp_path), "FAIL_FIRST": str(fail_first),
        "ORDER_BODY": order_body, "ORDER_HTTP": order_http, "SAMPLE_TYPES_JSON": samples, "HOME": os.environ.get("HOME", "/"),
    }
    result = subprocess.run(["bash", str(SCRIPT), "Bearer synthetic-test-token"], env=env, capture_output=True, text=True, timeout=60)
    log = (tmp_path / "posts.log")
    attempts = log.read_text().splitlines() if log.exists() else []
    return result, attempts


import shutil

import pytest

needs_tools = pytest.mark.skipif(not (shutil.which("jq") and shutil.which("uuidgen")), reason="jq and uuidgen are required")


@needs_tools
def test_two_transport_failures_then_success_uses_the_same_key_and_payload_each_time(tmp_path):
    result, attempts = run_script(tmp_path, fail_first=2)
    assert len(attempts) == 3, (attempts, result.stderr)                      # exactly three POSTs, no extra diagnostic POST
    keys = {a.split("key=")[1].split(" ")[0] for a in attempts}
    payloads = {a.split("payload=")[1].split(" ")[0] for a in attempts}
    assert len(keys) == 1 and len(payloads) == 1 and keys != {""}
    assert all("timeout=yes" in a for a in attempts)
    assert "Orden creada: HB-1" in result.stdout                              # the third attempt succeeded and the flow went on
    assert result.stderr.count("Intento") == 2


@needs_tools
def test_every_attempt_failing_stops_with_a_clear_nonzero_result_after_exactly_three_tries(tmp_path):
    result, attempts = run_script(tmp_path, fail_first=99)
    assert len(attempts) == 3
    assert result.returncode == 3
    assert "Sin respuesta del servidor tras 3 intentos" in result.stderr and "misma llave" in result.stderr
    assert "Orden creada" not in result.stdout


@needs_tools
def test_an_http_error_body_is_shown_and_never_retried_or_re_asked(tmp_path):
    result, attempts = run_script(tmp_path, fail_first=0, order_http="422", order_body='{"detail":{"code":"numbering_code_required","message":"A sample code is required"}}')
    assert len(attempts) == 1                                                 # one POST: the error body came with it
    assert result.returncode == 1 and "HTTP 422" in result.stdout and "numbering_code_required" in result.stdout


@needs_tools
def test_an_empty_sample_type_catalog_stops_before_any_post(tmp_path):
    result, attempts = run_script(tmp_path, fail_first=0, samples="")
    assert attempts == [] and result.returncode == 1
    assert "catálogo de tipos de muestra está vacío" in result.stderr
