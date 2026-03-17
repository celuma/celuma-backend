#!/bin/bash

# Script para verificar el flujo completo: orden → factura con precio del catálogo → pago → balance
# Uso: ./verify_billing_flow.sh <TOKEN>

set -e

# Configuración
BASE_URL="${BASE_URL:-http://localhost:8000}"
TOKEN="${1:-Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI1YzZmMjVmYi00ODUxLTQ5ZjctYWNlMi03MjE2NWM2MzRlZjAiLCJleHAiOjE3NzA4OTEyMzl9.bVpfH8zwKXG9hWxCIhH8rNRkWvmx6Dm9WQywWEZKzMg}"

echo "====================================="
echo "Verificación de flujo de facturación"
echo "====================================="
echo ""

# Colores para output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# 1. Obtener tipos de estudio
echo -e "${YELLOW}1. Obteniendo tipos de estudio...${NC}"
STUDY_TYPES=$(curl -s -H "Authorization: $TOKEN" "$BASE_URL/api/v1/study-types/")
echo "$STUDY_TYPES" | jq -r '.study_types[] | select(.is_active == true) | "\(.name) (\(.code)) - ID: \(.id)"' | head -3
STUDY_TYPE_ID=$(echo "$STUDY_TYPES" | jq -r '.study_types[] | select(.is_active == true) | .id' | head -1)
STUDY_TYPE_CODE=$(echo "$STUDY_TYPES" | jq -r '.study_types[] | select(.id == "'$STUDY_TYPE_ID'") | .code')
echo -e "${GREEN}✓ Usando tipo de estudio: $STUDY_TYPE_CODE (ID: $STUDY_TYPE_ID)${NC}"
echo ""

# 2. Verificar catálogo de precios
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

# 3. Obtener pacientes
echo -e "${YELLOW}3. Obteniendo pacientes...${NC}"
PATIENTS=$(curl -s -H "Authorization: $TOKEN" "$BASE_URL/api/v1/patients/")
PATIENT_ID=$(echo "$PATIENTS" | jq -r '.patients[0].id')
PATIENT_NAME=$(echo "$PATIENTS" | jq -r '.patients[0] | "\(.first_name) \(.last_name)"')
TENANT_ID=$(echo "$PATIENTS" | jq -r '.patients[0].tenant_id')
BRANCH_ID=$(echo "$PATIENTS" | jq -r '.patients[0].branch_id')
echo -e "${GREEN}✓ Usando paciente: $PATIENT_NAME (ID: $PATIENT_ID)${NC}"
echo ""

# 4. Crear orden (sin order_code, se genera automático)
echo -e "${YELLOW}4. Creando orden con tipo de estudio $STUDY_TYPE_CODE...${NC}"
ORDER_RESPONSE=$(curl -s -H "Authorization: $TOKEN" \
    -H "Content-Type: application/json" \
    -X POST "$BASE_URL/api/v1/laboratory/orders/unified" \
    -d '{
        "tenant_id": "'$TENANT_ID'",
        "branch_id": "'$BRANCH_ID'",
        "patient_id": "'$PATIENT_ID'",
        "study_type_id": "'$STUDY_TYPE_ID'",
        "requested_by": "Dr. Test",
        "notes": "Prueba de verificación de facturación",
        "samples": [{
            "sample_code": "TEST-'$(date +%s)'",
            "type": "OTRO",
            "notes": "Muestra de prueba"
        }]
    }')

ORDER_ID=$(echo "$ORDER_RESPONSE" | jq -r '.order.id')
ORDER_CODE=$(echo "$ORDER_RESPONSE" | jq -r '.order.order_code')
if [ -z "$ORDER_ID" ] || [ "$ORDER_ID" == "null" ]; then
    echo -e "${RED}✗ Error al crear orden${NC}"
    echo "$ORDER_RESPONSE" | jq '.'
    exit 1
fi
echo -e "${GREEN}✓ Orden creada: $ORDER_CODE (ID: $ORDER_ID)${NC}"
echo ""

# 5. Verificar factura auto-generada
echo -e "${YELLOW}5. Verificando factura auto-generada...${NC}"
sleep 1  # Pequeña pausa para asegurar que la factura se haya creado
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

# Verificar que el precio coincida
if [ "$INVOICE_TOTAL" == "$PRICE" ]; then
    echo -e "${GREEN}✓ El precio de la factura coincide con el catálogo${NC}"
else
    echo -e "${YELLOW}⚠ Advertencia: precio factura (\$$INVOICE_TOTAL) != precio catálogo (\$$PRICE)${NC}"
fi
echo ""

# 6. Registrar pago parcial
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

# 7. Verificar balance de la orden
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

# 8. Completar pago
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

# 9. Verificar estado final
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
