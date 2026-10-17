#!/bin/bash

# Script to verify the full flow: order → invoice with catalog price → payment → balance
# Usage: ./verify_billing_flow.sh <TOKEN>      (or: TOKEN='Bearer <jwt>' ./verify_billing_flow.sh)
#
# Creates a real order, invoice and payment: run it ONLY against a disposable/synthetic stack
# with its own lab, study type, price, patient and sample type. The token is never embedded.

set -e

# Configuration
BASE_URL="${BASE_URL:-http://localhost:8000}"
TOKEN="${1:-${TOKEN:-}}"
if [ -z "$TOKEN" ]; then
    echo "ERROR: falta el token. Uso: $0 'Bearer <jwt>'  (o exporta TOKEN)" >&2
    exit 2
fi
case "$TOKEN" in
    [Bb]earer\ *) ;;
    *) TOKEN="Bearer $TOKEN" ;;
esac

echo "====================================="
echo "Verificación de flujo de facturación"
echo "====================================="
echo ""

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# 1. Fetch study types
echo -e "${YELLOW}1. Obteniendo tipos de estudio...${NC}"
STUDY_TYPES=$(curl -s -H "Authorization: $TOKEN" "$BASE_URL/api/v1/study-types/")
echo "$STUDY_TYPES" | jq -r '.study_types[] | select(.is_active == true) | "\(.name) (\(.code)) - ID: \(.id)"' | head -3
STUDY_TYPE_ID=$(echo "$STUDY_TYPES" | jq -r '.study_types[] | select(.is_active == true) | .id' | head -1)
STUDY_TYPE_CODE=$(echo "$STUDY_TYPES" | jq -r '.study_types[] | select(.id == "'$STUDY_TYPE_ID'") | .code')
echo -e "${GREEN}✓ Usando tipo de estudio: $STUDY_TYPE_CODE (ID: $STUDY_TYPE_ID)${NC}"
echo ""

# 2. Verify price catalog
echo -e "${YELLOW}2. Verificando catálogo de precios activos...${NC}"
PRICE_CATALOG=$(curl -s -H "Authorization: $TOKEN" "$BASE_URL/api/v1/price-catalog/?is_active=true")
PRICE=$(echo "$PRICE_CATALOG" | jq -r --arg STUDY_TYPE_ID "$STUDY_TYPE_ID" '.prices[] | select(.study_type_id == $STUDY_TYPE_ID) | .unit_price' | head -1)
if [ -z "$PRICE" ] || [ "$PRICE" == "null" ]; then
    echo -e "${RED}⚠ No hay precio activo para este tipo de estudio${NC}"
    echo "Listando precios disponibles:"
    echo "$PRICE_CATALOG" | jq -r '.prices[] | "\(.study_type?.name): $\(.unit_price) \(.currency)"'
    exit 1
fi
echo -e "${GREEN}✓ Precio encontrado: \$$PRICE MXN${NC}"
echo ""

# 3. Fetch patients
echo -e "${YELLOW}3. Obteniendo pacientes...${NC}"
PATIENTS=$(curl -s -H "Authorization: $TOKEN" "$BASE_URL/api/v1/patients/")
PATIENT_ID=$(echo "$PATIENTS" | jq -r '(if type == "array" then . else .patients end)[0].id')
PATIENT_NAME=$(echo "$PATIENTS" | jq -r '(if type == "array" then . else .patients end)[0] | "\(.first_name) \(.last_name)"')
TENANT_ID=$(echo "$PATIENTS" | jq -r '(if type == "array" then . else .patients end)[0].tenant_id')
BRANCH_ID=$(echo "$PATIENTS" | jq -r '(if type == "array" then . else .patients end)[0].branch_id')
echo -e "${GREEN}✓ Usando paciente: $PATIENT_NAME (ID: $PATIENT_ID)${NC}"
echo ""

# 3b. Pick an ACTIVE sample type of this tenant (new samples require one; the retired `type` enum is refused)
echo -e "${YELLOW}3b. Obteniendo tipo de muestra activo...${NC}"
SAMPLE_TYPES=$(curl -s -H "Authorization: $TOKEN" "$BASE_URL/api/v1/sample-types/?active_only=true")
SAMPLE_TYPE_ID=$(echo "$SAMPLE_TYPES" | jq -r '.sample_types[0].id // empty')
SAMPLE_TYPE_CODE=$(echo "$SAMPLE_TYPES" | jq -r '.sample_types[0].code // empty')
if [ -z "$SAMPLE_TYPE_ID" ]; then
    echo -e "${RED}✗ El catálogo de tipos de muestra está vacío: crea o importa al menos un tipo activo en Configuración → Tipos de Muestra${NC}" >&2
    exit 1
fi
echo -e "${GREEN}✓ Usando tipo de muestra: $SAMPLE_TYPE_CODE (ID: $SAMPLE_TYPE_ID)${NC}"
echo ""

# 4. Create order (no order_code — auto-generated)
echo -e "${YELLOW}4. Creando orden con tipo de estudio $STUDY_TYPE_CODE...${NC}"
# Céluma 1.4 (#8): creation requires an Idempotency-Key. ONE key for this one logical request; a retry
# after a transport failure reuses it, so a response lost after the commit is replayed, not duplicated.
IDEMPOTENCY_KEY="$(uuidgen)"
ORDER_PAYLOAD='{
    "tenant_id": "'$TENANT_ID'",
    "branch_id": "'$BRANCH_ID'",
    "patient_id": "'$PATIENT_ID'",
    "study_type_id": "'$STUDY_TYPE_ID'",
    "requested_by": "Dr. Test",
    "notes": "Prueba de verificación de facturación",
    "samples": [{
        "sample_type_id": "'$SAMPLE_TYPE_ID'",
        "notes": "Muestra de prueba"
    }]
}'
# At most 3 guarded TRANSPORT attempts (connection failure, timeout), always with the same key and payload.
# Any HTTP answer, error bodies included, is definitive and is never retried or re-asked.
CURL="${CURL:-curl}"            # overridable only so the retry behaviour can be tested with a controlled wrapper
ORDER_RESPONSE=""
HTTP_CODE=""
for ATTEMPT in 1 2 3; do
    set +e
    RAW=$("$CURL" -sS --connect-timeout 5 --max-time 30 -w '\n%{http_code}' \
        -H "Authorization: $TOKEN" \
        -H "Content-Type: application/json" \
        -H "Idempotency-Key: $IDEMPOTENCY_KEY" \
        -X POST "$BASE_URL/api/v1/laboratory/orders/unified" \
        -d "$ORDER_PAYLOAD" 2>/dev/null)
    CURL_RC=$?
    set -e
    if [ "$CURL_RC" -eq 0 ]; then
        HTTP_CODE="${RAW##*$'\n'}"
        ORDER_RESPONSE="${RAW%$'\n'*}"
        break
    fi
    echo -e "${YELLOW}⚠ Intento $ATTEMPT/3 sin respuesta (curl código $CURL_RC); se reintenta con la misma Idempotency-Key${NC}" >&2
    [ "$ATTEMPT" -lt 3 ] && sleep 1
done
if [ -z "$HTTP_CODE" ]; then
    echo -e "${RED}✗ Sin respuesta del servidor tras 3 intentos; la orden pudo o no haberse creado. Repite con la misma llave: $IDEMPOTENCY_KEY${NC}" >&2
    exit 3
fi

ORDER_ID=$(echo "$ORDER_RESPONSE" | jq -r '.order.id')
ORDER_CODE=$(echo "$ORDER_RESPONSE" | jq -r '.order.order_code')
if [ -z "$ORDER_ID" ] || [ "$ORDER_ID" == "null" ]; then
    echo -e "${RED}✗ Error al crear orden (HTTP $HTTP_CODE)${NC}"
    echo "$ORDER_RESPONSE" | jq '.'
    exit 1
fi
echo -e "${GREEN}✓ Orden creada: $ORDER_CODE (ID: $ORDER_ID)${NC}"
echo ""

# 5. Verify auto-generated invoice
echo -e "${YELLOW}5. Verificando factura auto-generada...${NC}"
sleep 1  # Brief pause to ensure the invoice has been created
INVOICE=$(curl -s -H "Authorization: $TOKEN" "$BASE_URL/api/v1/billing/orders/$ORDER_ID/invoice")
INVOICE_ID=$(echo "$INVOICE" | jq -r '.id')
INVOICE_NUMBER=$(echo "$INVOICE" | jq -r '.invoice_number')
INVOICE_TOTAL=$(echo "$INVOICE" | jq -r '.total')
if [ -z "$INVOICE_ID" ] || [ "$INVOICE_ID" == "null" ]; then
    echo -e "${RED}✗ No se encontró factura para la orden${NC}"
    echo "$INVOICE" | jq '.'
    exit 1
fi
echo -e "${GREEN}✓ Factura generada: $INVOICE_NUMBER${NC}"
echo "  Total: \$$INVOICE_TOTAL MXN"
echo "  Items:"
echo "$INVOICE" | jq -r '.items[] | "    - \(.description): $\(.unit_price) x \(.quantity) = $\(.subtotal)"'

# Verify the price matches
if [ "$INVOICE_TOTAL" == "$PRICE" ]; then
    echo -e "${GREEN}✓ El precio de la factura coincide con el catálogo${NC}"
else
    echo -e "${YELLOW}⚠ Advertencia: precio factura (\$$INVOICE_TOTAL) != precio catálogo (\$$PRICE)${NC}"
fi
echo ""

# 6. Record partial payment
echo -e "${YELLOW}6. Registrando pago parcial de \$$(echo "scale=2; $INVOICE_TOTAL / 2" | bc) MXN...${NC}"
PARTIAL_PAYMENT=$(echo "scale=2; $INVOICE_TOTAL / 2" | bc)
PAYMENT_RESPONSE=$(curl -s -H "Authorization: $TOKEN" \
    -H "Content-Type: application/json" \
    -X POST "$BASE_URL/api/v1/billing/payments/" \
    -d '{
        "tenant_id": "'$TENANT_ID'",
        "invoice_id": "'$INVOICE_ID'",
        "amount": '$PARTIAL_PAYMENT',
        "currency": "MXN",
        "method": "cash",
        "reference": "TEST-PAYMENT-1"
    }')

PAYMENT_ID=$(echo "$PAYMENT_RESPONSE" | jq -r '.id')
if [ -z "$PAYMENT_ID" ] || [ "$PAYMENT_ID" == "null" ]; then
    echo -e "${RED}✗ Error al crear pago${NC}"
    echo "$PAYMENT_RESPONSE" | jq '.'
    exit 1
fi
echo -e "${GREEN}✓ Pago registrado (ID: $PAYMENT_ID)${NC}"
echo ""

# 7. Verify order balance
echo -e "${YELLOW}7. Verificando balance de la orden...${NC}"
BALANCE=$(curl -s -H "Authorization: $TOKEN" "$BASE_URL/api/v1/billing/orders/$ORDER_ID/balance")
echo "$BALANCE" | jq '{
    order_id,
    total_invoiced,
    total_paid,
    balance,
    is_locked,
    invoices: .invoices[] | {invoice_number, total, status, balance}
}'
REMAINING=$(echo "$BALANCE" | jq -r '.balance')
echo -e "${GREEN}✓ Balance pendiente: \$$REMAINING MXN${NC}"
echo ""

# 8. Complete payment
echo -e "${YELLOW}8. Completando pago con \$$REMAINING MXN...${NC}"
FINAL_PAYMENT=$(curl -s -H "Authorization: $TOKEN" \
    -H "Content-Type: application/json" \
    -X POST "$BASE_URL/api/v1/billing/payments/" \
    -d '{
        "tenant_id": "'$TENANT_ID'",
        "invoice_id": "'$INVOICE_ID'",
        "amount": '$REMAINING',
        "currency": "MXN",
        "method": "transfer",
        "reference": "TEST-PAYMENT-2"
    }')

FINAL_PAYMENT_ID=$(echo "$FINAL_PAYMENT" | jq -r '.id')
if [ -z "$FINAL_PAYMENT_ID" ] || [ "$FINAL_PAYMENT_ID" == "null" ]; then
    echo -e "${RED}✗ Error al crear pago final${NC}"
    echo "$FINAL_PAYMENT" | jq '.'
    exit 1
fi
echo -e "${GREEN}✓ Pago final registrado (ID: $FINAL_PAYMENT_ID)${NC}"
echo ""

# 9. Verify final state
echo -e "${YELLOW}9. Verificando estado final...${NC}"
FINAL_BALANCE=$(curl -s -H "Authorization: $TOKEN" "$BASE_URL/api/v1/billing/orders/$ORDER_ID/balance")
FINAL_REMAINING=$(echo "$FINAL_BALANCE" | jq -r '.balance')
IS_LOCKED=$(echo "$FINAL_BALANCE" | jq -r '.is_locked')
INVOICE_STATUS=$(echo "$FINAL_BALANCE" | jq -r '.invoices[0].status')

echo "Estado final:"
echo "  Balance: \$$FINAL_REMAINING MXN"
echo "  Orden bloqueada: $IS_LOCKED"
echo "  Estado factura: $INVOICE_STATUS"

if [ "$FINAL_REMAINING" == "0" ] || [ "$FINAL_REMAINING" == "0.00" ]; then
    echo -e "${GREEN}✓✓✓ Factura pagada completamente${NC}"
else
    echo -e "${YELLOW}⚠ Balance pendiente: \$$FINAL_REMAINING${NC}"
fi

if [ "$IS_LOCKED" == "false" ]; then
    echo -e "${GREEN}✓✓✓ Orden desbloqueada (acceso a reporte permitido)${NC}"
else
    echo -e "${RED}✗ Orden aún bloqueada${NC}"
fi

echo ""
echo -e "${GREEN}====================================="
echo "Verificación completada"
echo "=====================================${NC}"
echo ""
echo "Resumen:"
echo "  - Orden: $ORDER_CODE"
echo "  - Factura: $INVOICE_NUMBER"
echo "  - Total: \$$INVOICE_TOTAL MXN"
echo "  - Pagado: \$$INVOICE_TOTAL MXN"
echo "  - Balance: \$$FINAL_REMAINING MXN"
