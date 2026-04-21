CREATE TABLE default.ca_ms07_refactor_v3_temp AS
WITH calendario AS (
    -- NUESTRO TRUCO: Doble tipado para encender Kudu
    SELECT DISTINCT CAST(fecha_dia AS DATE) AS fecha_saldo_date,
                    CAST(fecha_dia AS VARCHAR(10)) AS fecha_saldo_str
    FROM pr_bsf_4con.productos_parametria_ft_calendario_saldo
    WHERE fecha_proceso = '20260420'

    UNION ALL

    SELECT
        CAST(
            CASE WHEN dayofweek(CAST(from_unixtime(unix_timestamp('20260420', 'yyyyMMdd'), 'yyyy-MM-dd') AS DATE)) = 2
                 THEN date_sub(CAST(from_unixtime(unix_timestamp('20260420', 'yyyyMMdd'), 'yyyy-MM-dd') AS DATE), 3)
                 ELSE date_sub(CAST(from_unixtime(unix_timestamp('20260420', 'yyyyMMdd'), 'yyyy-MM-dd') AS DATE), 1)
            END AS DATE
        ) AS fecha_saldo_date,
        CAST(
            CASE WHEN dayofweek(CAST(from_unixtime(unix_timestamp('20260420', 'yyyyMMdd'), 'yyyy-MM-dd') AS DATE)) = 2
                 THEN date_sub(CAST(from_unixtime(unix_timestamp('20260420', 'yyyyMMdd'), 'yyyy-MM-dd') AS DATE), 3)
                 ELSE date_sub(CAST(from_unixtime(unix_timestamp('20260420', 'yyyyMMdd'), 'yyyy-MM-dd') AS DATE), 1)
            END AS VARCHAR(10)
        ) AS fecha_saldo_str
    FROM (SELECT 1) dummy
    LEFT JOIN pr_bsf_4con.productos_parametria_ft_calendario_saldo t ON t.fecha_proceso = '20260420'
    WHERE t.fecha_proceso IS NULL
),
caja_ahorro AS (
    -- NUESTRO TRUCO: INNER JOIN nativo para evitar Full Scan en Kudu
    SELECT *
    FROM (
        SELECT camov.*, c.fecha_saldo_str,
               RANK() OVER(PARTITION BY camov.nro_cta, camov.id_sucursal, camov.id_cta ORDER BY camov.cbu DESC) AS sort_id
        FROM pr_bsf_4con.productos_caja_ahorro_dim_maestro camov
        INNER JOIN calendario c
           ON camov.fecha_saldo = c.fecha_saldo_date
    ) ranked
    WHERE sort_id = 1
),
ajus_cap AS (
    -- TRUCO MANU: Group By con MAX en lugar de DISTINCT
    SELECT id_sucursal, nro_cta, id_cta, MAX(mto_transaccion) as mto_transaccion
    FROM pr_bsf_4con.productos_caja_de_ahorro_ft_maestro_historico
    WHERE id_movimiento = 278
      AND id_cta IN (94, 6)
      AND fecha_valor = date_sub(cast(from_unixtime(unix_timestamp('20260420', 'yyyyMMdd'), 'yyyy-MM-dd') AS DATE), 1)
    GROUP BY id_sucursal, nro_cta, id_cta
),
t1 AS (
    -- TRUCO MANU: Reemplazo del RANK por filtro MAX correlacionado
    SELECT id_sucursal, CAST(nro_cta AS STRING) AS nro_cuenta, id_cta AS id_cuenta, fecha_valor,
           SUM(CASE WHEN CAST(id_movimiento AS INT) < 250 THEN mto_transaccion * (-1)
                    WHEN CAST(id_movimiento AS INT) > 749 THEN mto_transaccion * (-1)
                    ELSE mto_transaccion
               END) AS sumarizado
    FROM (
        SELECT *
        FROM pr_bsf_4con.productos_caja_de_ahorro_ft_maestro_historico mh
        WHERE mh.fecha_ingreso > mh.fecha_valor
          AND mh.fecha_valor = (
              SELECT MAX(i.fecha_valor)
              FROM pr_bsf_4con.productos_caja_de_ahorro_ft_maestro_historico i
              WHERE i.id_sucursal   = mh.id_sucursal
                AND i.id_cta        = mh.id_cta
                AND i.nro_cta       = mh.nro_cta
                AND i.id_movimiento = mh.id_movimiento
                AND i.fecha_ingreso > i.fecha_valor
          )
    ) latest
    GROUP BY id_sucursal, CAST(nro_cta AS STRING), id_cta, fecha_valor
),
FINAL AS (
    -- REFACTOR CONJUNTO: Eliminamos CROSS JOIN y conectamos todo directo
    SELECT CONCAT(camov.nro_cta) nro_cuenta,
           LPAD(TRIM(CAST(camov.id_cliente_core AS STRING)), 13, '0') AS id_cliente_core,
           CONCAT('1330', '0002', lpad(CAST(camov.id_moneda AS STRING), 3, '0'), lpad(CAST(camov.id_sucursal AS STRING), 5, '0'), lpad(CAST(camov.id_cta AS STRING), 6, '0'), '00000000000000', lpad(CAST(camov.nro_cta AS STRING), 12, '0')) id_cuenta,
           camov.fecha_saldo_str AS fecha_saldo,
           camov.fecha_ult_transaccion fecha_ultima_transaccion,
           camov.mto_saldo_efec_hoy + COALESCE(t1.sumarizado, 0) saldo_capital,
           t1.sumarizado,
           camov.intereses_devengados_act interes_devengado,
           CAST(NULL AS DECIMAL(15, 2)) interes_pagado,
           camov.mto_tasa_int_acreedor tasa_interes,
           COALESCE(dti.tasa_efectiva_mensual, 0) tasa_efectiva_mensual,
           COALESCE(dti.tasa_nominal_anual, 0) tasa_nominal_anual,
           camov.saldo_acreedor_prom_mes_act saldo_promedio_acreedor_mes_actual,
           camov.saldo_acreedor_prom_mes_ant saldo_promedio_acreedor_mes_anterior,
           camov.saldo_acreedor_prom_90_dias saldo_promedio_acreedor_90_dias,
           inte1.saldo_int_hasta AS interes_diario,
           movi.mto_transaccion AS ajuste_capital,
           camov.saldo_acreedor_prom_180_dias saldo_promedio_acreedor_180_dias,
           CAST(camov.id_cta AS INT) AS id_linea
    FROM caja_ahorro camov
    LEFT JOIN t1
      ON camov.id_sucursal = t1.id_sucursal
     AND camov.nro_cta = t1.nro_cuenta
     AND camov.id_cta = CAST(t1.id_cuenta AS VARCHAR(10))
     AND camov.fecha_saldo = t1.fecha_valor -- FIX: Comparamos nativo DATE vs DATE
    LEFT JOIN pr_bsf_4con.productos_ca_dim_tasas_interes dti
      ON camov.id_sucursal = dti.id_sucursal
     AND CAST(camov.id_cta AS VARCHAR(10)) = CAST(dti.id_tipo_cuenta AS VARCHAR(10))
     AND camov.nro_cta = CAST(dti.nro_cuenta AS VARCHAR(20))
     AND dti.fecha_proceso = '20260420'
    LEFT JOIN ajus_cap movi
      ON camov.nro_cta = cast(movi.nro_cta AS varchar(19))
     AND camov.id_sucursal = movi.id_sucursal
     AND camov.id_cta = cast(movi.id_cta AS varchar(10))
    LEFT JOIN pr_bsf_4con.productos_ca_ft_intereses_devengados inte1
      ON camov.nro_cta = cast(inte1.nro_cuenta AS varchar(20))
     AND camov.id_sucursal = inte1.id_sucursal
     AND camov.id_cta = cast(inte1.id_tipo_cuenta AS varchar(10))
     AND inte1.fecha_proceso = '20260420'
)
SELECT f.sumarizado, f.nro_cuenta, f.id_cliente_core, f.id_cuenta, f.fecha_saldo, cast(f.fecha_ultima_transaccion AS varchar(10)) fecha_ultima_transaccion, f.saldo_capital, f.interes_devengado, f.interes_pagado, cast(f.tasa_interes AS decimal(19, 6)) tasa_interes, cast(f.tasa_efectiva_mensual AS decimal(19, 6)) tasa_efectiva_mensual, cast(f.tasa_nominal_anual AS decimal(19, 6)) tasa_nominal_anual, f.interes_diario, f.ajuste_capital, f.saldo_promedio_acreedor_mes_actual, f.saldo_promedio_acreedor_mes_anterior, f.saldo_promedio_acreedor_90_dias, f.saldo_promedio_acreedor_180_dias, f.id_linea, '20260420' AS fecha_monitor
FROM FINAL f;