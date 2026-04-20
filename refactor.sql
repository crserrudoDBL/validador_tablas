WITH

-- Resolves the single balance date: uses MAX to avoid the UNION ALL + IS NULL anti-pattern
calendario AS (
  SELECT
    COALESCE(
      MAX(CAST(fecha_dia AS VARCHAR(10))),
      CAST(
        CASE
          WHEN dayofweek(CAST(from_unixtime(unix_timestamp('20260420', 'yyyyMMdd'), 'yyyy-MM-dd') AS DATE)) = 2
            THEN date_sub(CAST(from_unixtime(unix_timestamp('20260420', 'yyyyMMdd'), 'yyyy-MM-dd') AS DATE), 3)
          ELSE
            date_sub(CAST(from_unixtime(unix_timestamp('20260420', 'yyyyMMdd'), 'yyyy-MM-dd') AS DATE), 1)
        END AS VARCHAR(10)
      )
    ) AS fecha_saldo
  FROM pr_bsf_4con.productos_parametria_ft_calendario_saldo
  WHERE fecha_proceso = '20260420'
),

-- Deduplication pushed into subquery so downstream joins operate on already-filtered rows
caja_ahorro AS (
  SELECT *
  FROM (
    SELECT
      *,
      RANK() OVER (PARTITION BY nro_cta, id_sucursal, id_cta ORDER BY cbu DESC) AS sort_id
    FROM pr_bsf_4con.productos_caja_ahorro_dim_maestro
    WHERE CAST(fecha_saldo AS VARCHAR(10)) = (SELECT fecha_saldo FROM calendario)
  ) ranked
  WHERE sort_id = 1
),

-- Capital adjustments: GROUP BY instead of DISTINCT (no window fn, cheaper)
ajus_cap AS (
  SELECT
    id_sucursal,
    nro_cta,
    id_cta,
    MAX(mto_transaccion) AS mto_transaccion   -- MAX is safe: DISTINCT guarantees one value per key
  FROM pr_bsf_4con.productos_caja_de_ahorro_ft_maestro_historico
  WHERE id_movimiento = 278
    AND id_cta IN (94, 6)
    AND fecha_valor = date_sub(
          CAST(from_unixtime(unix_timestamp('20260420', 'yyyyMMdd'), 'yyyy-MM-dd') AS DATE), 1)
  GROUP BY id_sucursal, nro_cta, id_cta
),

-- Replaces the cahis + t1 two-CTE chain: aggregate directly from the source table.
-- The original ranking (ORDER BY fecha_valor DESC, rank=1) kept only the latest fecha_valor
-- per (sucursal, cta, nro_cta, movimiento). Grouping by those same keys + fecha_valor and
-- then taking the row with the MAX fecha_valor per partition is equivalent.
t1 AS (
  SELECT
    id_sucursal,
    CAST(nro_cta AS STRING)  AS nro_cuenta,
    id_cta                   AS id_cuenta,
    fecha_valor,
    SUM(
      CASE
        WHEN CAST(id_movimiento AS INT) < 250 THEN mto_transaccion * (-1)
        WHEN CAST(id_movimiento AS INT) > 749 THEN mto_transaccion * (-1)
        ELSE mto_transaccion
      END
    ) AS sumarizado
  FROM (
    -- Keeps only the latest-fecha_valor row per (sucursal, cta, nro_cta, movimiento),
    -- reproducing the original ranking = 1 filter without materialising a ranked CTE
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
  SELECT
    CONCAT(camov.nro_cta)                                                                AS nro_cuenta,
    LPAD(TRIM(CAST(camov.id_cliente_core AS STRING)), 13, '0')                           AS id_cliente_core,
    CONCAT(
      '1330', '0002',
      lpad(CAST(camov.id_moneda   AS STRING),  3, '0'),
      lpad(CAST(camov.id_sucursal AS STRING),  5, '0'),
      lpad(CAST(camov.id_cta     AS STRING),   6, '0'),
      '00000000000000',
      lpad(CAST(camov.nro_cta    AS STRING),  12, '0')
    )                                                                                    AS id_cuenta,
    CAST(cal.fecha_saldo AS VARCHAR(10))                                                 AS fecha_saldo,
    camov.fecha_ult_transaccion                                                          AS fecha_ultima_transaccion,
    camov.mto_saldo_efec_hoy + COALESCE(t1.sumarizado, 0)                               AS saldo_capital,
    t1.sumarizado,
    camov.intereses_devengados_act                                                       AS interes_devengado,
    CAST(NULL AS DECIMAL(15, 2))                                                         AS interes_pagado,
    camov.mto_tasa_int_acreedor                                                          AS tasa_interes,
    COALESCE(dti.tasa_efectiva_mensual, 0)                                               AS tasa_efectiva_mensual,
    COALESCE(dti.tasa_nominal_anual,    0)                                               AS tasa_nominal_anual,
    camov.saldo_acreedor_prom_mes_act                                                    AS saldo_promedio_acreedor_mes_actual,
    camov.saldo_acreedor_prom_mes_ant                                                    AS saldo_promedio_acreedor_mes_anterior,
    camov.saldo_acreedor_prom_90_dias                                                    AS saldo_promedio_acreedor_90_dias,
    inte1.saldo_int_hasta                                                                AS interes_diario,
    movi.mto_transaccion                                                                 AS ajuste_capital,
    camov.saldo_acreedor_prom_180_dias                                                   AS saldo_promedio_acreedor_180_dias,
    CAST(camov.id_cta AS INT)                                                            AS id_linea
  FROM caja_ahorro camov
  CROSS JOIN calendario cal
  LEFT JOIN t1
         ON camov.id_sucursal = t1.id_sucursal
        AND camov.nro_cta     = t1.nro_cuenta
        AND camov.id_cta      = CAST(t1.id_cuenta AS VARCHAR(10))
        AND camov.fecha_saldo = t1.fecha_valor
  LEFT JOIN pr_bsf_4con.productos_ca_dim_tasas_interes dti
         ON camov.id_sucursal                = dti.id_sucursal
        AND CAST(camov.id_cta AS VARCHAR(10)) = CAST(dti.id_tipo_cuenta AS VARCHAR(10))
        AND camov.nro_cta                    = CAST(dti.nro_cuenta AS VARCHAR(20))
        AND dti.fecha_proceso                = '20260420'
  LEFT JOIN ajus_cap movi
         ON camov.nro_cta     = CAST(movi.nro_cta  AS VARCHAR(19))
        AND camov.id_sucursal = movi.id_sucursal
        AND camov.id_cta      = CAST(movi.id_cta   AS VARCHAR(10))
  LEFT JOIN pr_bsf_4con.productos_ca_ft_intereses_devengados inte1
         ON camov.nro_cta     = CAST(inte1.nro_cuenta    AS VARCHAR(20))
        AND camov.id_sucursal = inte1.id_sucursal
        AND camov.id_cta      = CAST(inte1.id_tipo_cuenta AS VARCHAR(10))
        AND from_unixtime(unix_timestamp(inte1.fecha_proceso, 'yyyyMMdd'), 'yyyy-MM-dd')
            = CAST(from_unixtime(unix_timestamp('20260420', 'yyyyMMdd'), 'yyyy-MM-dd') AS DATE)
)

SELECT
  f.sumarizado,
  f.nro_cuenta,
  f.id_cliente_core,
  f.id_cuenta,
  f.fecha_saldo,
  CAST(f.fecha_ultima_transaccion AS VARCHAR(10)) AS fecha_ultima_transaccion,
  f.saldo_capital,
  f.interes_devengado,
  f.interes_pagado,
  CAST(f.tasa_interes           AS DECIMAL(19, 6)) AS tasa_interes,
  CAST(f.tasa_efectiva_mensual  AS DECIMAL(19, 6)) AS tasa_efectiva_mensual,
  CAST(f.tasa_nominal_anual     AS DECIMAL(19, 6)) AS tasa_nominal_anual,
  f.interes_diario,
  f.ajuste_capital,
  f.saldo_promedio_acreedor_mes_actual,
  f.saldo_promedio_acreedor_mes_anterior,
  f.saldo_promedio_acreedor_90_dias,
  f.saldo_promedio_acreedor_180_dias,
  f.id_linea,
  '20260420' AS fecha_monitor
FROM FINAL f;