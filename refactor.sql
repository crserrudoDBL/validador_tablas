WITH  
  calendario AS (
    SELECT 
      CASE 
        WHEN calensal.fecha_saldo IS NOT NULL THEN calensal.fecha_saldo 
        WHEN dayofweek(CAST(to_timestamp('20260619', 'yyyyMMdd') AS DATE)) = 2 THEN date_sub(CAST(to_timestamp('20260619', 'yyyyMMdd') AS DATE), 3) 
        ELSE date_sub(CAST(to_timestamp('20260619', 'yyyyMMdd') AS DATE), 1) 
      END AS fecha_saldo
    FROM (SELECT 1 x) t
    LEFT JOIN pr_bsj_4con.productos_parametria_ft_calendario_saldo calensal ON calensal.fecha_proceso = '20260619'
    LIMIT 1
  ),
  
  mae AS (
    SELECT 
      cta.id_cliente_core, cta.id_paquete, cta.nro_cuenta, cta.digito_verif, CAST(cta.id_sucursal AS INT) AS id_sucursal, 1 AS nro_concepto 
    FROM pr_bsj_4con.productos_cta_corriente_dim_maestro as cta 
    INNER JOIN calendario c ON cta.fecha_saldo = c.fecha_saldo
    WHERE cta.id_estado = 1 AND cta.marca_cta_ppal_paquete = 'S' AND cta.id_paquete != 0 
    UNION ALL 
    SELECT 
      maeca.id_cliente_core, maeca.id_paquete, CAST(maeca.nro_cta AS INT) AS nro_cuenta, CAST(maeca.digito_verificador AS SMALLINT) AS digito_verif, CAST(maeca.id_sucursal AS INT) AS id_sucursal, 2 AS nro_concepto 
    FROM pr_bsj_4con.productos_caja_ahorro_dim_maestro maeca 
    INNER JOIN calendario c ON maeca.fecha_saldo = c.fecha_saldo
    WHERE maeca.estado_cta = '1' AND maeca.marca_paquete = 'S'
  ), 
  
  cuentas AS (
    SELECT 
      h.nro_cuenta, h.id_sucursal, h.digito_verif, h.id_transaccion, h.id_causal, h.fecha_valor, h.mto_transaccion 
    FROM pr_bsj_4con.productos_cta_corriente_ft_maestro_historico h
    LEFT JOIN calendario on 1 = 1
    WHERE h.id_transaccion IN (109, 150) AND h.id_causal = 637 
      AND h.fecha_valor BETWEEN date_trunc('month', calendario.fecha_saldo) AND calendario.fecha_saldo
    UNION ALL 
    SELECT 
      h.nro_cta AS nro_cuenta, h.id_sucursal, h.digito_cta AS digito_verif, h.id_transaccion, h.id_causal_concepto AS id_causal, h.fecha_valor, h.mto_transaccion 
    FROM pr_bsj_4con.productos_caja_de_ahorro_ft_maestro_historico h
    LEFT JOIN calendario on 1 = 1
    WHERE h.id_transaccion IN (209, 250) AND h.id_causal_concepto = 637 
      AND h.fecha_valor BETWEEN date_trunc('month', calendario.fecha_saldo) AND calendario.fecha_saldo
  ), 
  
  comisiones AS (
    SELECT DISTINCT 
      p.nro_cuenta_db, p.sucursal_cuenta_db, p.digito_cuenta_db, p.concepto_cuenta_db, p.trx, p.cau, p.id_nro_cuenta, p.id_nro_sucursal, p.digito_verificador, p.nro_concepto, p.id_nro_paquete, p.motivo_no_cobrar, p.motivo_bonificacion
    FROM pr_bsj_4con.productos_paquetes_rel_pqtca_comisiones p
    LEFT JOIN calendario on 1 = 1
    WHERE p.cau = 637 AND p.trx IN (250, 150) AND (p.derivar_cobro_cta = 1 OR p.cobrar_cuenta = 1)
      AND CONCAT(CAST(p.anio AS STRING), '-', LPAD(CAST(p.mes AS STRING), 2, '0')) = 
          IF(
            SUBSTR(CAST(calendario.fecha_saldo AS STRING), 1, 7) = CONCAT(CAST(p.anio AS STRING), '-', LPAD(CAST(p.mes AS STRING), 2, '0')), 
            SUBSTR(CAST(calendario.fecha_saldo AS STRING), 1, 7), 
            SUBSTR(CAST(add_months(calendario.fecha_saldo, -1) AS STRING), 1, 7)
          )
  ), 
  
  FINAL_cuentas AS (
    SELECT 
      CAST(cuentas.fecha_valor AS VARCHAR(10)) AS fecha_cobro, 
      CONCAT('1', '045', '0077', '000', LPAD(CAST(mae.id_sucursal AS STRING), 5, '0'), '000000000000', LPAD(CAST(mae.id_paquete AS STRING), 2, '0'), LPAD(CAST(mae.nro_cuenta AS STRING), 11, '0'), CAST(mae.digito_verif AS STRING)) AS id_paquete, 
      CONCAT('1', '045', CASE WHEN pqtca.concepto_cuenta_db = 1 THEN '0001' WHEN pqtca.concepto_cuenta_db = 2 THEN '0002' END, '080', LPAD(CAST(pqtca.sucursal_cuenta_db AS STRING), 5, '0'), '000000', LPAD('0', 14, '0'), LPAD(CAST(pqtca.nro_cuenta_db AS VARCHAR(11)), 11, '0'), CAST(pqtca.digito_cuenta_db AS VARCHAR(1))) AS id_cuenta_cobro, 
      '045' AS codigo_banco, 'BSJ' AS desc_banco, affinity.mto_importe_comision AS comision_a_cobrar, pqtca.motivo_bonificacion, NULL AS motivo_no_cobro, affinity.meses_bonificados_1 AS plazo_bonificacion, NULL AS poliza, NULL AS ramo, 'Debito en cuenta' AS tipo_cobro, pqtca.motivo_no_cobrar, 
      SUM(CASE WHEN cuentas.id_transaccion IN (109, 209) THEN cuentas.mto_transaccion * -1 ELSE 0.00 END) AS comision_bonificada, 
      SUM(CASE WHEN cuentas.id_transaccion IN (250, 150) THEN cuentas.mto_transaccion ELSE 0.00 END) AS comision_cobrada 
    FROM cuentas 
    INNER JOIN comisiones pqtca ON pqtca.nro_cuenta_db = cuentas.nro_cuenta AND pqtca.sucursal_cuenta_db = cuentas.id_sucursal AND pqtca.cau = cuentas.id_causal AND pqtca.digito_cuenta_db = cuentas.digito_verif 
    INNER JOIN mae ON mae.nro_cuenta = pqtca.id_nro_cuenta AND mae.id_sucursal = pqtca.id_nro_sucursal AND mae.digito_verif = pqtca.digito_verificador AND mae.nro_concepto = pqtca.nro_concepto AND mae.id_paquete = pqtca.id_nro_paquete 
    LEFT JOIN (SELECT fecha_saldo, fecha_proceso FROM pr_bsj_4con.productos_parametria_ft_calendario_saldo) calensal ON calensal.fecha_saldo = cuentas.fecha_valor
    LEFT JOIN pr_bsj_4con.productos_paquetes_dim_parametros_affinity affinity ON mae.id_paquete = affinity.id_codigo_paquete AND calensal.fecha_proceso = affinity.fecha_proceso 
    GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13
  ), 
  
  comisones_tca AS (
    SELECT DISTINCT 
      p.nro_cuil, p.anio, p.mes, p.motivo_no_cobrar, p.id_nro_cuenta, p.id_nro_sucursal, p.digito_verificador, p.nro_concepto, p.id_nro_paquete, p.mto_bonificar, p.nro_sucursal_tarjeta_credito, p.nro_cuenta_tarjeta_credito, p.motivo_bonificacion 
    FROM pr_bsj_4con.productos_paquetes_rel_pqtca_comisiones p
    LEFT JOIN calendario ON 1 = 1
    WHERE p.fecha_proceso = '20260619' AND p.cobrar_tc = 1 
      AND CONCAT(CAST(p.anio AS STRING), '-', LPAD(CAST(p.mes AS STRING), 2, '0')) IN (
            SUBSTR(CAST(calendario.fecha_saldo AS STRING), 1, 7), 
            SUBSTR(CAST(add_months(calendario.fecha_saldo, -1) AS STRING), 1, 7)
          )
  ), 
  
  creden AS (
    SELECT 
      CASE WHEN t.nro_cuil IS NULL THEN SUBSTR(CAST(add_months(calendario.fecha_saldo, -1) AS STRING), 1, 7) ELSE SUBSTR(CAST(ca.fecha_ajuste AS STRING), 1, 7) END AS mes_join, 
      ca.fecha_ajuste AS fecha_cobro, 
      CASE WHEN ca.codigo_concepto = 8420 THEN ca.importe * -1 ELSE ca.importe END AS importe, 
      ca.codigo_concepto, CAST(ca.cuenta AS BIGINT) AS nro_cuenta_tarjeta_credito, CAST(cs.id_sucursal AS INT) AS nro_sucursal_tarjeta_credito, CAST(cs.cuit AS BIGINT) AS cuit 
    FROM pr_bsj_4con.productos_paquetes_dim_creden_ajus ca 
    LEFT JOIN calendario ON 1 = 1
    INNER JOIN pr_bsj_4con.productos_paquetes_dim_credenciales_socios cs ON CAST(ca.cuenta AS BIGINT) = CAST(cs.num_cuenta AS BIGINT) AND cs.fecha_proceso = '20260619' 
    LEFT JOIN comisones_tca t ON SUBSTR(CAST(ca.fecha_ajuste AS STRING), 1, 7) = CONCAT(CAST(t.anio AS STRING), '-', LPAD(CAST(t.mes AS STRING), 2, '0')) AND CAST(cs.cuit AS BIGINT) = CAST(t.nro_cuil AS BIGINT) 
    WHERE ca.codigo_concepto IN (6420, 8420) AND ca.fecha_ajuste BETWEEN date_trunc('month', calendario.fecha_saldo) AND calendario.fecha_saldo
  ), 
  
  visa AS (
    SELECT 
      CASE WHEN t.nro_cuil IS NULL THEN SUBSTR(CAST(add_months(calendario.fecha_saldo, -1) AS STRING), 1, 7) ELSE SUBSTR(CAST(va.fecha_ajuste AS STRING), 1, 7) END AS mes_join, 
      va.fecha_ajuste AS fecha_cobro, vs.nro_cuit, 
      CASE WHEN va.codigo_concepto = 3805 THEN va.importe * -1 ELSE va.importe END AS importe, 
      va.codigo_concepto, CAST(va.cuenta AS BIGINT) AS nro_cuenta_tarjeta_credito, CAST(vs.cod_sucur AS INT) AS nro_sucursal_tarjeta_credito 
    FROM pr_bsj_4con.productos_paquetes_dim_visa_historico_ajuste va 
    LEFT JOIN calendario ON 1 = 1
    INNER JOIN pr_bsj_4con.productos_paquetes_dim_visa_socios vs ON CAST(va.cuenta AS BIGINT) = CAST(vs.id_cuenta AS BIGINT) AND vs.fecha_proceso = '20260619' 
    LEFT JOIN comisones_tca t ON SUBSTR(CAST(va.fecha_ajuste AS STRING), 1, 7) = CONCAT(CAST(t.anio AS STRING), '-', LPAD(CAST(t.mes AS STRING), 2, '0')) AND CAST(REPLACE(vs.nro_cuit, '-', '') AS BIGINT) = CAST(t.nro_cuil AS BIGINT) 
    WHERE va.codigo_concepto IN (3805, 1294) AND va.fecha_ajuste BETWEEN date_trunc('month', calendario.fecha_saldo) AND calendario.fecha_saldo
  ), 
  
  tarjeta AS (
    SELECT 
      comi.nro_cuil, comi.motivo_no_cobrar, comi.id_nro_cuenta, comi.id_nro_sucursal, comi.digito_verificador, comi.nro_concepto, comi.id_nro_paquete, 
      NVL(CAST(comi.nro_sucursal_tarjeta_credito AS INT), dos.nro_sucursal_tarjeta_credito) AS nro_sucursal_tarjeta_credito, 
      NVL(CAST(comi.nro_cuenta_tarjeta_credito AS BIGINT), dos.nro_cuenta_tarjeta_credito) AS nro_cuenta_tarjeta_credito, 
      comi.motivo_bonificacion, dos.fecha_cobro, 
      SUM(CASE WHEN CAST(dos.codigo_concepto AS INT) IN (8420, 3805) THEN dos.importe ELSE 0.00 END) AS mto_bonificar, 
      SUM(dos.importe) AS importe 
    FROM comisones_tca comi 
    LEFT JOIN calendario ON 1 = 1
    INNER JOIN (
        SELECT fecha_cobro, mes_join, cuit AS nro_cuit, importe, codigo_concepto, nro_cuenta_tarjeta_credito, nro_sucursal_tarjeta_credito FROM creden 
        UNION ALL 
        SELECT fecha_cobro, mes_join, CAST(REPLACE(nro_cuit, '-', '') AS BIGINT) AS nro_cuit, importe, codigo_concepto, nro_cuenta_tarjeta_credito, nro_sucursal_tarjeta_credito FROM visa
    ) dos ON dos.mes_join = CONCAT(CAST(comi.anio AS STRING), '-', LPAD(CAST(comi.mes AS STRING), 2, '0')) AND CAST(dos.nro_cuit AS BIGINT) = CAST(comi.nro_cuil AS BIGINT) 
    WHERE dos.fecha_cobro BETWEEN TRUNC(calendario.fecha_saldo, 'MONTH') AND calendario.fecha_saldo 
    GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11
  ), 
  
  FINAL_tarjeta AS (
    SELECT 
      tarjeta.fecha_cobro, 
      CONCAT('1', '045', '0077', '000', LPAD(CAST(NVL(mae.id_sucursal, tarjeta.id_nro_sucursal) AS STRING), 5, '0'), '000000', '000000000000', LPAD(CAST(NVL(mae.id_paquete, tarjeta.id_nro_paquete) AS STRING), 2, '0'), LPAD(CAST(NVL(mae.nro_cuenta, tarjeta.id_nro_cuenta) AS STRING), 11, '0'), CAST(NVL(mae.digito_verif, tarjeta.digito_verificador) AS STRING)) AS id_paquete, 
      CONCAT('1', '045', '0025', '080', LPAD(CAST(tarjeta.nro_sucursal_tarjeta_credito AS VARCHAR(5)), 5, '0'), '000000', LPAD('0', 14, '0'), LPAD(CAST(tarjeta.nro_cuenta_tarjeta_credito AS VARCHAR(11)), 11, '0'), '0') AS id_cuenta_cobro, 
      '045' AS codigo_banco, 'BSJ' AS desc_banco, affinity.mto_importe_comision AS comision_a_cobrar, tarjeta.motivo_bonificacion, NULL AS motivo_no_cobro, affinity.meses_bonificados_1 AS plazo_bonificacion, NULL AS poliza, NULL AS ramo, 'Debito en tarjeta' AS tipo_cobro, tarjeta.motivo_no_cobrar, 
      SUM(tarjeta.mto_bonificar) AS comision_bonificada, SUM(tarjeta.importe) AS comision_cobrada 
    FROM tarjeta 
    LEFT JOIN (SELECT fecha_saldo, fecha_proceso FROM pr_bsj_4con.productos_parametria_ft_calendario_saldo) calensal ON calensal.fecha_saldo = tarjeta.fecha_cobro
    LEFT JOIN mae ON mae.nro_cuenta = tarjeta.id_nro_cuenta AND mae.id_sucursal = tarjeta.id_nro_sucursal AND mae.digito_verif = tarjeta.digito_verificador AND mae.nro_concepto = tarjeta.nro_concepto AND mae.id_paquete = tarjeta.id_nro_paquete 
    LEFT JOIN pr_bsj_4con.productos_paquetes_dim_parametros_affinity affinity ON tarjeta.id_nro_paquete = affinity.id_codigo_paquete AND calensal.fecha_proceso = affinity.fecha_proceso 
    GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13
  ), 
  
  final_total AS (
    SELECT fecha_cobro, id_paquete, id_cuenta_cobro, codigo_banco, desc_banco, comision_a_cobrar, motivo_bonificacion, motivo_no_cobro, plazo_bonificacion, poliza, ramo, tipo_cobro, motivo_no_cobrar, SUM(comision_bonificada) AS comision_bonificada, SUM(comision_cobrada) AS comision_cobrada FROM FINAL_cuentas GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13 
    UNION ALL 
    SELECT CAST(fecha_cobro AS VARCHAR(10)) AS fecha_cobro, id_paquete, id_cuenta_cobro, codigo_banco, desc_banco, comision_a_cobrar, motivo_bonificacion, motivo_no_cobro, plazo_bonificacion, poliza, ramo, tipo_cobro, motivo_no_cobrar, SUM(comision_bonificada) AS comision_bonificada, SUM(comision_cobrada) AS comision_cobrada FROM FINAL_tarjeta WHERE id_paquete IS NOT NULL AND id_cuenta_cobro IS NOT NULL AND fecha_cobro IS NOT NULL GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13
  )

SELECT 
  CAST(t.fecha_cobro AS VARCHAR(10)) AS fecha_cobro, 
  CAST(t.id_paquete AS VARCHAR(48)) AS id_paquete, 
  CAST(t.id_cuenta_cobro AS VARCHAR(48)) AS id_cuenta_cobro, 
  CAST(t.codigo_banco AS INT) AS codigo_banco, 
  CAST(t.desc_banco AS VARCHAR(3)) AS desc_banco, 
  CAST(t.comision_a_cobrar AS DECIMAL(19, 2)) AS comision_a_cobrar, 
  CAST(SUM(t.comision_bonificada) AS DECIMAL(19, 2)) AS comision_bonificada, 
  CAST(SUM(t.comision_cobrada) AS DECIMAL(19, 2)) AS comision_cobrada, 
  CAST(CASE WHEN t.comision_cobrada IS NULL THEN 'No se cobró' ELSE 'OK' END AS VARCHAR(12)) AS estado_cobro, 
  CAST(t.motivo_bonificacion AS VARCHAR(30)) AS motivo_bonificacion, 
  CAST(t.motivo_no_cobrar AS VARCHAR(30)) AS motivo_no_cobro, 
  CAST(t.plazo_bonificacion AS INT) AS plazo_bonificacion, 
  CAST(t.poliza AS VARCHAR(30)) AS poliza, 
  CAST(t.ramo AS VARCHAR(30)) AS ramo, 
  CAST(t.tipo_cobro AS VARCHAR(20)) AS tipo_cobro, 
  CAST('20260619' AS VARCHAR(8)) AS fecha_monitor,
  CAST(CASE WHEN month(CAST(t.fecha_cobro AS DATE)) = month(cal.fecha_saldo) AND year(cal.fecha_saldo) = 2026 AND month(cal.fecha_saldo) = 6 THEN cal.fecha_saldo ELSE last_day(CAST(t.fecha_cobro AS DATE)) END AS varchar(10)) AS fecha 
FROM final_total t
LEFT JOIN calendario cal ON 1 = 1
GROUP BY 1, 2, 3, 4, 5, 6, 9, 10, 11, 12, 13, 14, 15, 16, 17
