# Scripts de Verificación

Este directorio contiene scripts para verificar funcionalidad del sistema.

## verify_billing_flow.sh

Script para verificar el flujo completo de facturación: orden → factura automática con precio del catálogo → pago → balance.

### Requisitos

- Backend corriendo (Docker o local)
- Token de autenticación válido
- `jq` instalado (para procesar JSON)
- `bc` instalado (para cálculos decimales)

### Uso

```bash
# Con token por defecto (incluido en el script)
./verify_billing_flow.sh

# Con token personalizado
./verify_billing_flow.sh "Bearer YOUR_TOKEN_HERE"

# Con URL personalizada
BASE_URL=http://localhost:8000 ./verify_billing_flow.sh "Bearer YOUR_TOKEN"
```

### ¿Qué hace el script?

1. **Obtiene tipos de estudio activos** y selecciona uno para la prueba
2. **Verifica precios en catálogo** para el tipo de estudio seleccionado
3. **Obtiene pacientes** y selecciona uno para la prueba
4. **Crea una orden** sin especificar `order_code` (se genera automáticamente: ej. IHQ-1)
5. **Verifica factura auto-generada** con el precio correcto del catálogo
6. **Registra pago parcial** y verifica el balance
7. **Completa el pago** restante
8. **Verifica estado final**: balance 0, factura PAID, orden desbloqueada

### Ejemplo de salida

```
=====================================
Verificación de flujo de facturación
=====================================

1. Obteniendo tipos de estudio...
Inmunohistoquímica (IHQ) - ID: abc-123
Biopsia (BIOPSIA) - ID: def-456
✓ Usando tipo de estudio: IHQ (ID: abc-123)

2. Verificando catálogo de precios activos...
✓ Precio encontrado: $1500.00 MXN

3. Obteniendo pacientes...
✓ Usando paciente: Juan Pérez (ID: patient-123)

4. Creando orden con tipo de estudio IHQ...
✓ Orden creada: IHQ-1 (ID: order-123)

5. Verificando factura auto-generada...
✓ Factura generada: INV-IHQ-1
  Total: $1500.00 MXN
  Items:
    - Inmunohistoquímica: $1500.00 x 1 = $1500.00
✓ El precio de la factura coincide con el catálogo

6. Registrando pago parcial de $750.00 MXN...
✓ Pago registrado (ID: pay-123)

7. Verificando balance de la orden...
✓ Balance pendiente: $750.00 MXN

8. Completando pago con $750.00 MXN...
✓ Pago final registrado (ID: pay-124)

9. Verificando estado final...
Estado final:
  Balance: $0.00 MXN
  Orden bloqueada: false
  Estado factura: PAID
✓✓✓ Factura pagada completamente
✓✓✓ Orden desbloqueada (acceso a reporte permitido)

=====================================
Verificación completada
=====================================

Resumen:
  - Orden: IHQ-1
  - Factura: INV-IHQ-1
  - Total: $1500.00 MXN
  - Pagado: $1500.00 MXN
  - Balance: $0.00 MXN
```

### Notas

- El script crea datos de prueba (orden, muestras, pagos) en el sistema
- Útil para verificar que todo el flujo funciona correctamente después de cambios
- Se puede ejecutar en cualquier momento contra un sistema funcional
- Los datos creados permanecen en la base de datos (no son borrados automáticamente)
