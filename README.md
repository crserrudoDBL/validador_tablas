# Validador de Equivalencia en Impala

Script para validar que una query original y su refactor devuelvan el mismo resultado funcional.

Soporta dos modos:

- `pairs`: compara tablas ya existentes (compatible con el flujo anterior).
- `sql`: recibe dos archivos SQL, crea tablas temporales, compara, y elimina temporales al final.

Ademas, siempre genera un reporte humano en TXT con el resultado por pasos.

## Que valida

Para cada par ejecuta:

1. `count(*)`
2. `anti-join` por clave (`A-B` y `B-A`)
3. `hash checksum` de filas (auditoria rapida)
4. comparacion estricta full-row con multiplicidad

Incluye resultados por par:

- `FAST_AUDIT_RESULT` (OK/DIFF)
- `STRICT_100_RESULT` (OK/DIFF)

Tambien incluye muestras de diferencias lado a lado (hasta 50 por lado) cuando hay mismatch.

## Flujo de dos pasos

1. **Step 1 - equivalencia funcional**
	- Regla de exito: `STRICT_100_RESULT = OK`.
	- Si falla, el reporte marca FAIL y muestra muestras `A_ONLY` vs `B_ONLY` en formato lado a lado.

2. **Step 2 - comparacion de eficiencia (solo si Step 1 pasa)**
	- Re-ejecuta ambas queries en modo read-only para comparar tiempo y recursos.
	- Metodo usado: ejecucion directa de cada query (sin `CREATE`/`INSERT`/`DROP` en Step 2) para medir tiempo y recursos sobre la query real.
	- Ejecuta cada query varias veces (`--step2-runs`, por defecto `5`) con orden alternado por ronda para reducir sesgo por estado del cluster.
	- Compara por mediana de tiempo/memoria/CPU y muestra series por corrida.

## Modo 1: comparar tablas existentes (`pairs`)

```bash
python comparar_tablas.py \
	--mode pairs \
	--pairs table_pairs.csv \
	--output validacion_status.sql \
	--impala-opts "-i 172.30.215.49:21000" \
	--run \
	--result-output resultado_status.txt
```

### Formato de `table_pairs.csv`

Columnas esperadas:

- `pair_name`: nombre del caso de comparacion
- `original_table`: tabla version original
- `refactor_table`: tabla version candidata
- `key_columns`: columnas de clave separadas por `;` (opcional si usas `--auto-columns`)

Ejemplo:

```csv
pair_name,original_table,refactor_table,key_columns
saldo_diario,db_prod.saldo_orig,db_cand.saldo_cand,id_cuenta;fecha_saldo
```

## Modo 2: comparar query original vs refactor (`sql`)

En este modo el script:

1. Lee `--original-sql` y `--refactor-sql` (una unica query `SELECT` por archivo; CTEs permitidas).
2. Crea dos tablas temporales con `CREATE TABLE ... AS <query>`.
3. Ejecuta la comparacion funcional sobre esas tablas (Step 1).
4. Si Step 1 pasa, ejecuta Step 2 en read-only con ambas queries originales.
5. Hace cleanup con `DROP TABLE IF EXISTS` en ambos temporales (incluso si hay error).

Ejemplo:

```bash
python comparar_tablas.py \
	--mode sql \
	--original-sql elastic.sql \
	--refactor-sql refactor.sql \
	--pair-name validacion_ms06 \
	--temp-db default \
	--temp-prefix cmp_tmp \
	--impala-opts "-i 172.30.215.49:21000" \
	--output out.sql \
	--result-output resultado_sql.txt
```

## Metricas de tiempo, memoria y CPU

Por cada paso de query (crear temporal original, crear temporal refactor, comparacion, drops) se registran:

- tiempo wall-clock (`elapsed_wall_sec`)
- `query_id` cuando se detecta en salida de `impala-shell`
- memoria pico y CPU (si se puede obtener)

Fuentes de metricas:

1. API web de Impala (preferida): `--impala-web-url` o inferida desde `--impala-opts`.
2. Fallback: parseo best-effort del texto de profile en salida de `impala-shell`.

## Reporte humano TXT

Por defecto se genera `comparison_report.txt` con:

- resumen de Step 1 por par (PASS/FAIL y metricas clave)
- muestras lado a lado en caso de mismatch (`A_ONLY` vs `B_ONLY`)
- resumen de Step 2 (si aplica) con comparacion original vs refactor por medianas

Puedes cambiar la ruta de salida:

```bash
python comparar_tablas.py ... --human-report reporte_humano.txt
```

Guardar metricas en JSON:

```bash
python comparar_tablas.py ... --metrics-json metricas.json
```

## Opciones importantes

- `--mode auto|pairs|sql`: selecciona el modo (`auto` detecta por presencia de SQL files).
- `--auto-columns` / `--no-auto-columns`: autodescubrimiento de columnas clave.
- `--key-columns`: clave explicita en modo `sql` (separadas por `;`).
- `--impala-shell`: comando/ruta de `impala-shell`.
- `--impala-opts`: opciones de conexion (`-i`, kerberos, etc.).
- `--run`: ejecuta el SQL generado (en modo `sql` se ejecuta siempre).
- `--result-output`: guarda stdout de ejecucion.
- `--impala-web-url`: URL base para metricas API (ej: `http://host:25000`).
- `--impala-web-timeout`: timeout de llamadas HTTP.
- `--metrics-json`: salida JSON de metricas.
- `--human-report`: salida TXT legible (por defecto `comparison_report.txt`).
- `--step2-runs`: cantidad de corridas por query en Step 2 (por defecto `5`, en orden alternado).

## Windows: error comun con impala-shell

Si aparece un error como "No se encontro el comando 'impala-shell' en PATH", indica la ruta completa:

```powershell
python comparar_tablas.py ... --impala-shell "C:/ruta/impala-shell.cmd"
```

## Nota tecnica

El hash y la comparacion estricta se calculan usando solo las columnas en comun detectadas por `DESCRIBE`.
