"""Corre los escenarios (1 a 8) como en la presentación y guarda la evidencia.

- Una sola conversación por pasada (igual que en la UI), con pausa de "narración" entre escenarios.
- Cada escenario se califica automáticamente (citas inventadas, cifras sin respaldo, tool equivocada,
  decidir solo que el pedido no procede, etc.).

Uso:
  python scripts/run_scenarios.py --pasadas 2 --sin-respaldo     # pruebas sin tocar la cuota del respaldo
  python scripts/run_scenarios.py --proveedor gemini              # otra combinación
Salida: evidencia/evidencia_<proveedor>_<fecha>.md (y .json)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402
from langgraph.types import Command  # noqa: E402

from seed import reset_db  # noqa: E402
from src import config as cfg  # noqa: E402
from src.costs import CostTracker  # noqa: E402
from src.erp_client import ERPClient  # noqa: E402
from src.escenarios import CLIENTE_DEMO, ESCENARIOS  # noqa: E402
from src.graph import RETRY_HOOKS, build_graph, mensajes_del_turno, pendiente_de_aprobacion  # noqa: E402


def _tabla(filas: list[dict], columnas: list[str]) -> str:
    if not filas:
        return "_(vacío)_\n"
    out = "| " + " | ".join(columnas) + " |\n|" + "---|" * len(columnas) + "\n"
    for f in filas:
        out += "| " + " | ".join(str(f.get(c, "")).replace("|", "/").replace("\n", " ")[:110] for c in columnas) + " |\n"
    return out


def _tools_llamadas(msgs) -> list[tuple[str, dict]]:
    return [(tc["name"], tc["args"]) for m in msgs if isinstance(m, AIMessage) for tc in m.tool_calls]


def calificar(esc_id: int, st: dict, msgs_turno, llamadas_llm: int, aprob: dict | None) -> list[str]:
    """Devuelve la lista de fallas (vacía = escenario limpio)."""
    fallas = []
    rf = st.get("respuesta_final") or {}
    texto = re.sub(r"[    ]", " ", rf.get("respuesta", ""))
    traza = [p for p in st.get("traza", []) if p["turno"] == st.get("turno")]
    for p in traza:
        if p["nodo"] == "grounding_check" and p["ok"] is False:
            fallas.append(f"grounding: {p['detalle']}")
        if p["nodo"] == "agente" and "Límite" in p["detalle"]:
            fallas.append("llegó al límite de iteraciones")
    tools = _tools_llamadas(msgs_turno)
    nombres = [t for t, _ in tools]
    if esc_id == 1:
        if "calcular_cotizacion" not in nombres:
            fallas.append("no usó calcular_cotizacion")
        if "buscar_politicas" not in nombres:
            fallas.append("no consultó las políticas")
        if not re.search(r"\b8\s?%", texto):
            fallas.append("no informa el 8%")
        if not re.search(r"48\s*(a|y|[-–‑‐—])\s*72", texto):
            fallas.append("no informa 48 a 72 horas")
        if rf.get("escalar_a_asesor"):
            fallas.append("escaló sin motivo")
    elif esc_id == 2:
        if not aprob:
            fallas.append("no llegó a aprobación humana")
        else:
            if aprob["total"] != 1235.0:
                fallas.append(f"total {aprob['total']} != 1235")
            if aprob["pedidos_en_erp_antes_de_aprobar"] != 3 or aprob["pedidos_en_erp_despues_de_aprobar"] != 4:
                fallas.append("el ERP no cambió como se esperaba")
    elif esc_id == 3:
        borradores = [a for t, a in tools if t == "preparar_borrador_pedido"]
        if not borradores:
            fallas.append("decidió solo que no procede (no envió el borrador al validador)")
        elif (st.get("validacion") or {}).get("aprobado", True):
            fallas.append("el validador no bloqueó")
        if aprob:
            fallas.append("llegó a aprobación")
        if not re.search(r"4[.,\s]?000", texto):
            fallas.append("no propone la alternativa de 4.000 tallos")
    elif esc_id == 4:
        if "buscar_politicas" not in nombres:
            fallas.append("no consultó la guía de postcosecha")
        if not re.search(r"10\s*(a|y|[-–‑‐—])\s*14", texto):
            fallas.append("no informa 10 a 14 días")
        if rf.get("escalar_a_asesor"):
            fallas.append("escaló sin motivo")
    elif esc_id == 5:
        if not rf.get("escalar_a_asesor") or "No tengo esa información" not in texto:
            fallas.append("no escaló con la frase esperada")
    elif esc_id == 6:
        if not st.get("bloqueado") or llamadas_llm:
            fallas.append("el guardrail no bloqueó sin LLM")
    elif esc_id == 7:
        if "ver_catalogo" not in nombres:
            fallas.append("no usó ver_catalogo")
        if rf.get("escalar_a_asesor"):
            fallas.append("escaló en vez de mostrar el catálogo")
    elif esc_id == 8:
        if not aprob:
            fallas.append("no llegó a aprobación humana")
        elif aprob["total"] != 950.0:
            fallas.append(f"total {aprob['total']} != 950 (5 cajas = 2000 tallos con 5%)")
    return fallas


def correr_pasada(graph, n: int, pausa: float, erp: ERPClient) -> list[dict]:
    reset_db()
    thread_id = f"pasada{n}-{uuid.uuid4().hex[:6]}"
    cliente = erp.get_cliente(CLIENTE_DEMO["whatsapp"])  # el número de WhatsApp identifica al cliente
    reintentos: list[str] = []
    RETRY_HOOKS[thread_id] = lambda k, s, m: (reintentos.append(f"[{k}] {s:.1f}s {m[:90]}"),
                                              print(f"   ⏳ {'respaldo' if k == 0 else f'reintento {k}'} ({s:.1f}s) {m[:90]}"))
    resultados = []
    for i, esc in enumerate(ESCENARIOS):
        tracker = CostTracker()
        conf = {"configurable": {"thread_id": thread_id}, "callbacks": [tracker]}
        n_reint = len(reintentos)
        print(f"\n[P{n}] === {esc['titulo']} ===")
        t0 = time.time()
        error, aprob = None, None
        try:
            graph.invoke({"messages": [HumanMessage(content=esc["mensaje"])], "cliente_canal": cliente}, conf)
            pend = pendiente_de_aprobacion(graph, thread_id)
            if pend:
                v = pend["validacion"]
                aprob = {"total": v["total"], "pedidos_en_erp_antes_de_aprobar": len(erp.listar_pedidos())}
                graph.invoke(Command(resume={"accion": "aprobar", "usuario": "supervisor.maria"}), conf)
                aprob["pedidos_en_erp_despues_de_aprobar"] = len(erp.listar_pedidos())
        except Exception as e:  # noqa: BLE001
            error = f"{type(e).__name__}: {e}"
        seg = round(time.time() - t0, 1)
        st = graph.get_state({"configurable": {"thread_id": thread_id}}).values
        msgs_turno = mensajes_del_turno(st.get("messages", []))
        rf = st.get("respuesta_final") or {}
        fallas = ([f"error: {error[:150]}"] if error else []) + calificar(
            esc["id"], st, msgs_turno, len(tracker.llamadas), aprob)
        traza = [p for p in st.get("traza", []) if p["turno"] == st.get("turno")]
        modelos = [p.get("modelo") or "?" for p in traza if p["nodo"] == "agente" and "Límite" not in p["detalle"]]
        print(f"   ← {rf.get('respuesta', '')[:300]}")
        print(f"   {'✅ LIMPIO' if not fallas else '❌ ' + '; '.join(fallas)} · {seg}s · {tracker.total_tokens} tokens · {modelos}")
        resultados.append({
            "pasada": n, "escenario": esc, "fallas": fallas, "segundos": seg, "respuesta_final": rf,
            "traza": traza, "modelos": modelos, "costos": tracker.resumen(), "llamadas": tracker.llamadas,
            "aprobacion": aprob, "reintentos": reintentos[n_reint:], "error": error,
            "tools": _tools_llamadas(msgs_turno),
        })
        if i < len(ESCENARIOS) - 1:
            time.sleep(pausa)
    return resultados


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proveedor", default=None)
    ap.add_argument("--sin-respaldo", action="store_true", help="no usar LLM_FALLBACK_PROVIDER")
    ap.add_argument("--pasadas", type=int, default=1)
    ap.add_argument("--pausa", type=float, default=20.0, help="segundos de 'narración' entre escenarios")
    args = ap.parse_args()
    proveedor = args.proveedor or cfg.proveedor_actual()

    respaldo = None if args.sin_respaldo else cfg.get_llm_respaldo(proveedor)
    graph = build_graph(cfg.get_llm(proveedor), respaldo=respaldo)
    nombre_resp = cfg.proveedor_respaldo(proveedor) if respaldo else "ninguno"
    print(f"Principal: {proveedor} ({cfg.modelo_de(proveedor)}) · respaldo: {nombre_resp} · pausa {args.pausa}s")
    erp = ERPClient()
    from src.rag import precalentar
    precalentar()  # igual que la UI: el modelo de embeddings se carga antes de la demo

    todas, extra = [], {}
    for n in range(1, args.pasadas + 1):
        todas += correr_pasada(graph, n, args.pausa, erp)
        extra[n] = {"pedidos": erp.listar_pedidos(), "auditoria": list(reversed(erp.auditoria(100)))}
        if n < args.pasadas:
            time.sleep(args.pausa)

    salida = ROOT / "evidencia"
    salida.mkdir(exist_ok=True)
    marca = datetime.now().strftime("%Y%m%d_%H%M")
    base = salida / f"evidencia_{proveedor}_{marca}"
    base.with_suffix(".json").write_text(json.dumps(
        {"proveedor": proveedor, "modelo": cfg.modelo_de(proveedor), "respaldo": nombre_resp, "pausa": args.pausa,
         "resultados": todas, "erp_por_pasada": extra}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    filas = [{
        "Pasada": r["pasada"], "Escenario": r["escenario"]["titulo"],
        "Resultado": "✅ limpio" if not r["fallas"] else "❌ " + "; ".join(r["fallas"]),
        "Seg": r["segundos"], "Llamadas": r["costos"]["llamadas_llm"], "Tokens": r["costos"]["total_tokens"],
        "Modelo por paso": " → ".join(r["modelos"]) or "— (sin LLM)",
        "Reintentos": len(r["reintentos"]),
    } for r in todas]
    md = [f"# Evidencia: {args.pasadas} pasada(s) de los {len(ESCENARIOS)} escenarios\n\n"
          f"Principal **{proveedor}** (`{cfg.modelo_de(proveedor)}`) · respaldo **{nombre_resp}** · "
          f"pausa de narración {args.pausa:.0f} s · {marca}\n\n## Resumen\n\n",
          _tabla(filas, list(filas[0].keys()))]
    for r in todas:
        e = r["escenario"]
        md.append(f"\n## Pasada {r['pasada']} · {e['titulo']}\n\n**Cliente:** {e['mensaje']}\n\n"
                  f"**Respuesta:** {r['respuesta_final'].get('respuesta', '')}\n\n"
                  f"**Fuentes:** {', '.join(r['respuesta_final'].get('fuentes', [])) or '—'}\n\n")
        if r["aprobacion"]:
            a = r["aprobacion"]
            md.append(f"**Aprobación humana:** pedidos en ERP antes = {a['pedidos_en_erp_antes_de_aprobar']}, "
                      f"después = {a.get('pedidos_en_erp_despues_de_aprobar')}\n\n")
        md.append(_tabla(r["traza"], ["hora", "nodo", "detalle", "ok", "modelo", "segundos"]))
        if r["reintentos"]:
            md.append("\nReintentos: " + "; ".join(r["reintentos"]) + "\n")
    ult = extra[args.pasadas]
    md.append("\n## ERP al final de la última pasada\n\n" + _tabla(ult["pedidos"], ["id", "fecha", "cliente", "estado", "total", "creado_por", "aprobado_por"]))
    md.append("\n## Auditoría de la última pasada\n\n" + _tabla(ult["auditoria"], ["id", "timestamp", "usuario", "accion", "resultado"]))
    base.with_suffix(".md").write_text("".join(md), encoding="utf-8")
    limpias = [all(not r["fallas"] for r in todas if r["pasada"] == n) for n in range(1, args.pasadas + 1)]
    print(f"\nPasadas limpias: {limpias}\nEvidencia: {base.with_suffix('.md')}")


if __name__ == "__main__":
    main()
