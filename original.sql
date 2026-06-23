with 
  calendario AS (
    WITH 
      fecha_base AS (
        SELECT 
          CAST(
            from_unixtime(
              unix_timestamp('20260514', 'yyyyMMdd'), 
              'yyyy-MM-dd'
            ) AS DATE
          ) AS fecha_base
      ) 
    SELECT 
      CASE WHEN calensal.fecha_saldo IS NOT NULL THEN calensal.fecha_saldo WHEN dayofweek(fecha_base) = 2 THEN date_sub(fecha_base, 3) ELSE date_sub(fecha_base, 1) END AS fecha_saldo 
    FROM 
      fecha_base 
      LEFT JOIN pr_bsj_4con.productos_parametria_ft_calendario_saldo calensal ON calensal.fecha_proceso = '20260514'
  ), 
  mae AS (
    SELECT 
      cta.fecha_saldo AS fecha_saldo, 
      cta.id_cliente_core AS id_cliente_core, 
      cta.id_moneda AS id_moneda, 
      cta.id_producto AS id_producto, 
      cta.nro_cuenta as nro_cuenta, 
      cta.digito_verif as digito_verif, 
      CAST(cta.id_sucursal AS INT) AS id_sucursal, 
      cta.id_paquete, 
      1 AS nro_concepto 
    FROM 
      pr_bsj_4con.productos_cta_corriente_dim_maestro as cta 
    WHERE 
      fecha_saldo = (
        SELECT 
          fecha_saldo 
        from 
          calendario
      ) 
      AND id_estado = 1 
      AND marca_cta_ppal_paquete = 'S' 
      AND id_paquete != 0 
    UNION ALL 
    SELECT 
      maeca.fecha_saldo AS fecha_saldo, 
      maeca.id_cliente_core AS id_cliente_core, 
      maeca.id_moneda AS id_moneda, 
      cast(maeca.id_producto AS SMALLINT) AS id_producto, 
      cast(maeca.nro_cta as int) AS nro_cuenta, 
      cast(
        maeca.digito_verificador as smallint
      ) AS digito_verif, 
      CAST(maeca.id_sucursal AS INT) AS id_sucursal, 
      maeca.id_paquete, 
      2 AS nro_concepto 
    FROM 
      pr_bsj_4con.productos_caja_ahorro_dim_maestro maeca 
    WHERE 
      maeca.fecha_saldo = (
        SELECT 
          fecha_saldo 
        from 
          calendario
      ) 
      AND estado_cta = '1' 
      AND marca_paquete = 'S'
  ), 
  CC as (
    SELECT 
      distinct clave_registro, 
      nro_cuenta, 
      id_sucursal, 
      id_transaccion, 
      id_causal, 
      digito_verif, 
      fecha_valor, 
      mto_transaccion 
    from 
      pr_bsj_4con.productos_cta_corriente_ft_maestro_historico 
      LEFT JOIN calendario on 1 = 1 
    where 
      id_transaccion IN (109, 150) 
      and id_causal = 637 
      and fecha_valor between date_trunc('month', calendario.fecha_saldo) 
      and calendario.fecha_saldo
  ), 
  ca as (
    SELECT 
      distinct nro_transaccion as clave_registro, 
      nro_cta as nro_cuenta, 
      id_sucursal, 
      id_transaccion, 
      id_causal_concepto as id_causal, 
      digito_cta as digito_verif, 
      fecha_valor, 
      mto_transaccion 
    from 
      pr_bsj_4con.productos_caja_de_ahorro_ft_maestro_historico 
      LEFT JOIN calendario on 1 = 1 
    where 
      id_transaccion IN (209, 250) 
      and id_causal_concepto = 637 
      and fecha_valor between date_trunc('month', calendario.fecha_saldo) 
      and calendario.fecha_saldo
  ), 
  cuentas as (
    SELECT 
      * 
    from 
      CC 
    union all 
    SELECT 
      * 
    from 
      ca
  ), 
  comisiones as (
    SELECT 
      DISTINCT nro_cuenta_db, 
      sucursal_cuenta_db, 
      digito_cuenta_db, 
      concepto_cuenta_db, 
      trx, 
      cau, 
      anio, 
      mes, 
      id_nro_cuenta, 
      id_nro_sucursal, 
      digito_verificador, 
      nro_concepto, 
      id_nro_paquete, 
      mto_bonificar, 
      motivo_no_cobrar, 
      motivo_bonificacion 
    FROM 
      pr_bsj_4con.productos_paquetes_rel_pqtca_comisiones 
      left join calendario on 1 = 1 
    WHERE 
      concat(
        cast(anio as string), 
        '-', 
        lpad(
          cast(mes as string), 
          2, 
          '0'
        )
      ) = if(
        substr(
          cast(calendario.fecha_saldo as string), 
          1, 
          7
        ) = concat(
          cast(anio as string), 
          '-', 
          lpad(
            cast(mes as string), 
            2, 
            '0'
          )
        ), 
        substr(
          cast(calendario.fecha_saldo as string), 
          1, 
          7
        ), 
        substr(
          cast(
            add_months(calendario.fecha_saldo, -1) as string
          ), 
          1, 
          7
        )
      ) 
      and (
        derivar_cobro_cta = 1 
        or cobrar_cuenta = 1
      ) 
      AND cau = 637 
      and trx in (250, 150)
  ), 
  FINAL_cuentas as (
    SELECT 
      cast(
        cuentas.fecha_valor as varchar(10)
      ) as fecha_cobro, 
      concat(
        '1', 
        '045', 
        '0077', 
        '000', 
        lpad(
          cast(mae.id_sucursal AS string), 
          5, 
          '0'
        ), 
        '000000', 
        '000000000000', 
        lpad(
          cast(mae.id_paquete AS string), 
          2, 
          '0'
        ), 
        lpad(
          cast(mae.nro_cuenta AS string), 
          11, 
          '0'
        ), 
        CAST(mae.digito_verif AS STRING)
      ) id_paquete, 
      concat(
        '1', 
        '045', 
        CASE WHEN pqtca.concepto_cuenta_db = 1 THEN '0001' WHEN pqtca.concepto_cuenta_db = 2 THEN '0002' END, 
        '080', 
        lpad(
          CAST(
            pqtca.sucursal_cuenta_db AS STRING
          ), 
          5, 
          '0'
        ), 
        '000000', 
        lpad('0', 14, '0'), 
        lpad(
          CAST(
            pqtca.nro_cuenta_db AS VARCHAR(11)
          ), 
          11, 
          '0'
        ), 
        CAST(
          pqtca.digito_cuenta_db AS VARCHAR(1)
        )
      ) AS id_cuenta_cobro, 
      '045' AS codigo_banco, 
      'BSJ' AS desc_banco, 
      affinity.mto_importe_comision AS comision_a_cobrar, 
      pqtca.motivo_bonificacion AS motivo_bonificacion, 
      NULL AS motivo_no_cobro, 
      affinity.meses_bonificados_1 AS plazo_bonificacion, 
      NULL AS poliza, 
      NULL AS ramo, 
      'Debito en cuenta' as tipo_cobro, 
      pqtca.motivo_no_cobrar, 
      sum(
        CASE WHEN cuentas.id_transaccion IN (109, 209) THEN cuentas.mto_transaccion * -1 ELSE 0.00 END
      ) AS comision_bonificada, 
      sum(
        CASE WHEN cuentas.id_transaccion IN (250, 150) THEN cuentas.mto_transaccion ELSE 0.00 END
      ) as comision_cobrada 
    FROM 
      cuentas 
      left join pr_bsj_4con.productos_parametria_ft_calendario_saldo as calensal ON (fecha_saldo = fecha_valor) 
      INNER JOIN comisiones as pqtca ON (
        pqtca.nro_cuenta_db = cuentas.nro_cuenta 
        AND pqtca.sucursal_cuenta_db = cuentas.id_sucursal 
        AND pqtca.cau = cuentas.id_causal 
        AND pqtca.digito_cuenta_db = cuentas.digito_verif
      ) 
      INNER JOIN mae on (
        mae.nro_cuenta = pqtca.id_nro_cuenta 
        AND mae.id_sucursal = pqtca.id_nro_sucursal 
        AND mae.digito_verif = pqtca.digito_verificador 
        AND mae.nro_concepto = pqtca.nro_concepto 
        AND mae.id_paquete = pqtca.id_nro_paquete
      ) 
      LEFT JOIN pr_bsj_4con.productos_paquetes_dim_parametros_affinity affinity ON(
        mae.id_paquete = affinity.id_codigo_paquete 
        AND calensal.fecha_proceso = affinity.fecha_proceso
      ) 
    GROUP BY 
      1, 
      2, 
      3, 
      4, 
      5, 
      6, 
      7, 
      8, 
      9, 
      10, 
      11, 
      12, 
      13
  ), 
  comisones_tca as (
    select 
      distinct nro_cuil, 
      anio, 
      mes, 
      motivo_no_cobrar, 
      id_nro_cuenta, 
      id_nro_sucursal, 
      digito_verificador, 
      nro_concepto, 
      id_nro_paquete, 
      mto_bonificar, 
      nro_sucursal_tarjeta_credito, 
      nro_cuenta_tarjeta_credito, 
      motivo_bonificacion 
    from 
      pr_bsj_4con.productos_paquetes_rel_pqtca_comisiones 
      left join calendario on 1 = 1 
    WHERE 
      fecha_proceso = '20260514' 
      AND concat(
        cast(anio as string), 
        '-', 
        lpad(
          cast(mes as string), 
          2, 
          '0'
        )
      ) in (
        substr(
          cast(calendario.fecha_saldo as string), 
          1, 
          7
        ), 
        substr(
          CAST(
            add_months(calendario.fecha_saldo,-1) AS STRING
          ), 
          1, 
          7
        )
      ) 
      and cobrar_tc = 1
  ), 
  creden as(
    select 
      Case when comisones_tca.nro_cuil is null then substr(
        CAST(
          add_months(calendario.fecha_saldo,-1) AS STRING
        ), 
        1, 
        7
      ) else substr(
        CAST(ca.fecha_ajuste AS STRING), 
        1, 
        7
      ) end as mes_join, 
      ca.fecha_ajuste as fecha_cobro, 
      CASE WHEN ca.codigo_concepto = 8420 THEN ca.importe *-1 ELSE ca.importe END AS importe, 
      ca.codigo_concepto, 
      CAST(ca.cuenta AS BIGINT) AS nro_cuenta_tarjeta_credito, 
      CAST(cs.id_sucursal AS INT) as nro_sucursal_tarjeta_credito, 
      cast(cs.cuit as bigint) as cuit 
    from 
      pr_bsj_4con.productos_paquetes_dim_creden_ajus AS ca 
      left join calendario on 1 = 1 
      INNER JOIN pr_bsj_4con.productos_paquetes_dim_credenciales_socios as cs ON (
        CAST(ca.cuenta AS BIGINT) = CAST(cs.num_cuenta AS BIGINT) 
        AND cs.fecha_proceso = '20260514'
      ) 
      left join comisones_tca on (
        substr(
          CAST(ca.fecha_ajuste AS STRING), 
          1, 
          7
        ) = concat(
          cast(comisones_tca.anio as string), 
          '-', 
          lpad(
            cast(comisones_tca.mes as string), 
            2, 
            '0'
          )
        ) 
        and cast(cs.cuit as bigint) = cast(comisones_tca.nro_cuil as bigint)
      ) 
    WHERE 
      ca.codigo_concepto IN (6420, 8420) 
      AND ca.fecha_ajuste between date_trunc('month', calendario.fecha_saldo) 
      and calendario.fecha_saldo
  ), 
  visa as (
    select 
      Case when comisones_tca.nro_cuil is null then substr(
        CAST(
          add_months(calendario.fecha_saldo,-1) AS STRING
        ), 
        1, 
        7
      ) else substr(
        CAST(va.fecha_ajuste AS STRING), 
        1, 
        7
      ) end as mes_join, 
      va.fecha_ajuste as fecha_cobro, 
      vs.nro_cuit, 
      CASE WHEN va.codigo_concepto = 3805 THEN va.importe * -1 ELSE va.importe END AS importe, 
      va.codigo_concepto, 
      CAST(va.cuenta AS BIGINT) AS nro_cuenta_tarjeta_credito, 
      CAST(vs.cod_sucur AS INT) as nro_sucursal_tarjeta_credito 
    from 
      pr_bsj_4con.productos_paquetes_dim_visa_historico_ajuste AS va 
      LEFT JOIN calendario ON 1 = 1 
      INNER JOIN pr_bsj_4con.productos_paquetes_dim_visa_socios AS vs ON (
        CAST(va.cuenta AS BIGINT) = CAST(vs.id_cuenta AS BIGINT) 
        AND vs.fecha_proceso = '20260514'
      ) 
      left join comisones_tca on (
        substr(
          CAST(va.fecha_ajuste AS STRING), 
          1, 
          7
        ) = concat(
          cast(comisones_tca.anio as string), 
          '-', 
          lpad(
            cast(comisones_tca.mes as string), 
            2, 
            '0'
          )
        ) 
        and cast(
          replace(vs.nro_cuit, '-', '') as bigint
        ) = cast(comisones_tca.nro_CUIL as bigint)
      ) 
    WHERE 
      va.codigo_concepto IN (3805, 1294) 
      AND va.fecha_ajuste between date_trunc('month', calendario.fecha_saldo) 
      and calendario.fecha_saldo
  ), 
  tarjeta as (
    Select 
      comi.nro_cuil, 
      comi.motivo_no_cobrar, 
      comi.id_nro_cuenta, 
      comi.id_nro_sucursal, 
      comi.digito_verificador, 
      comi.nro_concepto, 
      comi.id_nro_paquete, 
      NVL(
        CAST(
          comi.nro_sucursal_tarjeta_credito AS int
        ), 
        dos.nro_sucursal_tarjeta_credito
      ) as nro_sucursal_tarjeta_credito, 
      NVL(
        CAST(
          comi.nro_cuenta_tarjeta_credito AS BIGINT
        ), 
        dos.nro_cuenta_tarjeta_credito
      ) as nro_cuenta_tarjeta_credito, 
      comi.motivo_bonificacion, 
      dos.fecha_cobro, 
      sum(
        CASE when CAST(dos.codigo_concepto AS INT) = 8420 
        or CAST(dos.codigo_concepto AS INT) = 3805 THEN dos.importe ELSE 0.00 end
      ) as mto_bonificar, 
      sum(dos.importe) as importe 
    from 
      comisones_tca as comi 
      left join CALENDARIO on 1 = 1 
      inner join (
        Select 
          creden.fecha_cobro, 
          creden.mes_join, 
          creden.cuit as nro_cuit, 
          creden.importe, 
          creden.codigo_concepto, 
          nro_cuenta_tarjeta_credito, 
          nro_sucursal_tarjeta_credito 
        from 
          creden 
        union all 
        Select 
          visa.fecha_cobro, 
          visa.mes_join, 
          CAST(
            replace(visa.nro_cuit, '-', '') AS BIGINT
          ) as nro_cuit, 
          visa.importe, 
          visa.codigo_concepto, 
          nro_cuenta_tarjeta_credito, 
          nro_sucursal_tarjeta_credito 
        from 
          visa
      ) dos on (
        dos.mes_join = concat(
          cast(comi.anio as string), 
          '-', 
          lpad(
            cast(comi.mes as string), 
            2, 
            '0'
          )
        ) 
        and cast(dos.nro_cuit as bigint) = cast(comi.nro_cuil as bigint)
      ) 
    WHERE 
      dos.fecha_cobro BETWEEN TRUNC(calendario.fecha_saldo, 'MONTH') 
      AND calendario.fecha_saldo 
    GROUP BY 
      1, 
      2, 
      3, 
      4, 
      5, 
      6, 
      7, 
      8, 
      9, 
      10, 
      11
  ), 
  FINAL_tarjeta AS (
    SELECT 
      tarjeta.fecha_cobro as fecha_cobro, 
      concat(
        '1', 
        '045', 
        '0077', 
        '000', 
        lpad(
          cast(
            NVL(
              mae.id_sucursal, tarjeta.id_nro_sucursal
            ) AS string
          ), 
          5, 
          '0'
        ), 
        '000000', 
        '000000000000', 
        lpad(
          cast(
            NVL(
              mae.id_paquete, tarjeta.id_nro_paquete
            ) AS string
          ), 
          2, 
          '0'
        ), 
        lpad(
          cast(
            NVL(
              mae.nro_cuenta, tarjeta.id_nro_cuenta
            ) AS string
          ), 
          11, 
          '0'
        ), 
        CAST(
          NVL(
            mae.digito_verif, tarjeta.digito_verificador
          ) AS STRING
        )
      ) AS id_paquete, 
      CONCAT(
        '1', 
        '045', 
        '0025', 
        '080', 
        lpad(
          CAST(
            tarjeta.nro_sucursal_tarjeta_credito AS VARCHAR(5)
          ), 
          5, 
          '0'
        ), 
        '000000', 
        lpad('0', 14, '0'), 
        lpad(
          CAST(
            tarjeta.nro_cuenta_tarjeta_credito AS VARCHAR(11)
          ), 
          11, 
          '0'
        ), 
        '0'
      ) AS id_cuenta_cobro, 
      '045' AS codigo_banco, 
      'BSJ' AS desc_banco, 
      affinity.mto_importe_comision AS comision_a_cobrar, 
      tarjeta.motivo_bonificacion AS motivo_bonificacion, 
      NULL AS motivo_no_cobro, 
      affinity.meses_bonificados_1 AS plazo_bonificacion, 
      NULL AS poliza, 
      NULL AS ramo, 
      'Debito en tarjeta' AS tipo_cobro, 
      tarjeta.motivo_no_cobrar, 
      SUM(tarjeta.mto_bonificar) AS comision_bonificada, 
      SUM(tarjeta.importe) as comision_cobrada 
    FROM 
      tarjeta 
      left join pr_bsj_4con.productos_parametria_ft_calendario_saldo as calensal ON (fecha_saldo = fecha_cobro) 
      LEFT JOIN mae on (
        mae.nro_cuenta = tarjeta.id_nro_cuenta 
        AND mae.id_sucursal = tarjeta.id_nro_sucursal 
        AND mae.digito_verif = tarjeta.digito_verificador 
        AND mae.nro_concepto = tarjeta.nro_concepto 
        AND mae.id_paquete = tarjeta.id_nro_paquete
      ) 
      LEFT JOIN pr_bsj_4con.productos_paquetes_dim_parametros_affinity affinity ON(
        tarjeta.id_nro_paquete = affinity.id_codigo_paquete 
        AND calensal.fecha_proceso = affinity.fecha_proceso
      ) 
    GROUP BY 
      1, 
      2, 
      3, 
      4, 
      5, 
      6, 
      7, 
      8, 
      9, 
      10, 
      11, 
      12, 
      13
  ), 
  final_total as (
    SELECT 
      fecha_cobro, 
      id_paquete, 
      id_cuenta_cobro, 
      codigo_banco, 
      desc_banco, 
      comision_a_cobrar, 
      motivo_bonificacion, 
      motivo_no_cobro, 
      plazo_bonificacion, 
      poliza, 
      ramo, 
      tipo_cobro, 
      motivo_no_cobrar, 
      SUM(comision_bonificada) AS comision_bonificada, 
      SUM(comision_cobrada) AS comision_cobrada 
    FROM 
      FINAL_cuentas 
    group by 
      fecha_cobro, 
      id_paquete, 
      id_cuenta_cobro, 
      codigo_banco, 
      desc_banco, 
      comision_a_cobrar, 
      motivo_bonificacion, 
      motivo_no_cobro, 
      plazo_bonificacion, 
      poliza, 
      ramo, 
      tipo_cobro, 
      motivo_no_cobrar 
    UNION ALL 
    SELECT 
      CAST(
        fecha_cobro AS VARCHAR(10)
      ) AS fecha_cobro, 
      id_paquete, 
      id_cuenta_cobro, 
      codigo_banco, 
      desc_banco, 
      comision_a_cobrar, 
      motivo_bonificacion, 
      motivo_no_cobro, 
      plazo_bonificacion, 
      poliza, 
      ramo, 
      tipo_cobro, 
      motivo_no_cobrar, 
      SUM(comision_bonificada) AS comision_bonificada, 
      SUM(comision_cobrada) AS comision_cobrada 
    FROM 
      FINAL_tarjeta 
    WHERE 
      id_paquete IS NOT NULL 
      AND id_cuenta_cobro IS NOT NULL 
      AND fecha_cobro IS NOT NULL 
    group by 
      fecha_cobro, 
      id_paquete, 
      id_cuenta_cobro, 
      codigo_banco, 
      desc_banco, 
      comision_a_cobrar, 
      motivo_bonificacion, 
      motivo_no_cobro, 
      plazo_bonificacion, 
      poliza, 
      ramo, 
      tipo_cobro, 
      motivo_no_cobrar
  ) 
SELECT 
  CAST(
    fecha_cobro AS VARCHAR(10)
  ) AS fecha_cobro, 
  CAST(
    id_paquete AS VARCHAR(48)
  ) AS id_paquete, 
  CAST(
    id_cuenta_cobro AS VARCHAR(48)
  ) AS id_cuenta_cobro, 
  CAST(codigo_banco AS INT) AS codigo_banco, 
  CAST(
    desc_banco AS VARCHAR(3)
  ) AS desc_banco, 
  CAST(
    comision_a_cobrar AS DECIMAL(19, 2)
  ) AS comision_a_cobrar, 
  CAST(
    SUM(comision_bonificada) AS DECIMAL(19, 2)
  ) AS comision_bonificada, 
  CAST(
    SUM(comision_cobrada) AS DECIMAL(19, 2)
  ) AS comision_cobrada, 
  CAST(
    CASE WHEN comision_cobrada IS NULL THEN 'No se cobró' ELSE 'OK' END AS VARCHAR(12)
  ) AS estado_cobro, 
  CAST(
    motivo_bonificacion AS VARCHAR(30)
  ) AS motivo_bonificacion, 
  CAST(
    motivo_no_cobrar AS VARCHAR(30)
  ) AS motivo_no_cobro, 
  CAST(plazo_bonificacion AS INT) AS plazo_bonificacion, 
  CAST(
    poliza AS VARCHAR(30)
  ) AS poliza, 
  CAST(
    ramo AS VARCHAR(30)
  ) AS ramo, 
  CAST(
    tipo_cobro AS VARCHAR(20)
  ) AS tipo_cobro, 
  CAST(
    '20260514' AS VARCHAR(8)
  ) AS fecha_monitor, 
  cast(
    CASE WHEN month(
      CAST(fecha_cobro AS DATE)
    ) = month(cal.fecha_saldo) 
    AND year(cal.fecha_saldo) = year(now()) 
    AND month(cal.fecha_saldo) = month(now()) THEN cal.fecha_saldo ELSE last_day(
      CAST(fecha_cobro AS DATE)
    ) END as varchar(10)
  ) as fecha 
FROM 
  final_total 
  left join calendario cal on 1 = 1 
group by 
  1, 
  2, 
  3, 
  4, 
  5, 
  6, 
  9, 
  10, 
  11, 
  12, 
  13, 
  14, 
  15, 
  16, 
  17
