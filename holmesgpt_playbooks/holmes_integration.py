"""
Playbook Robusta para integración con HolmesGPT
Envía problemas de pods a HolmesGPT para análisis de causa raíz con IA
"""
import logging
import requests
from hikaru.model.rel_1_26 import Pod
from robusta.api import (
    action,
    PodEvent,
    Finding,
    FindingType,
    FindingSeverity,
    MarkdownBlock,
)

logger = logging.getLogger(__name__)

HOLMESGPT_URL = "http://holmesgpt-holmes.holmesgpt.svc.cluster.local:80"


def _obtener_contexto_pod(event: PodEvent, pod: Pod) -> dict:
    """Recolecta contexto del pod para HolmesGPT"""
    # Eventos del pod
    try:
        pod_events = event.list_pod_events()
        eventos = [{"tipo": e.type, "razon": e.reason, "mensaje": e.message} for e in pod_events[:5]]
    except:
        eventos = []
    
    # Logs del pod (últimas 1000 caracteres)
    try:
        logs = event.get_pod_logs()
        logs = logs[-1000:] if len(logs) > 1000 else logs
    except:
        logs = ""
    
    # Estado de contenedores
    contenedores = []
    if pod.status.containerStatuses:
        for cs in pod.status.containerStatuses:
            info = {
                "nombre": cs.name,
                "imagen": cs.image,
                "ready": cs.ready,
                "reintentos": cs.restartCount
            }
            
            if cs.state:
                if cs.state.waiting:
                    info["estado"] = {
                        "esperando": {
                            "razon": cs.state.waiting.reason,
                            "mensaje": cs.state.waiting.message or ""
                        }
                    }
                elif cs.state.terminated:
                    info["estado"] = {
                        "terminado": {
                            "razon": cs.state.terminated.reason,
                            "codigo_salida": cs.state.terminated.exitCode,
                            "mensaje": cs.state.terminated.message or ""
                        }
                    }
            
            contenedores.append(info)
    
    return {
        "eventos": eventos,
        "logs": logs,
        "contenedores": contenedores,
        "fase": pod.status.phase
    }


@action
def analizar_pod_con_holmesgpt(event: PodEvent):
    """
    Analiza cualquier problema de pod usando HolmesGPT AI.
    Solicita diagnóstico en español con causa raíz y solución.
    """
    pod: Pod = event.get_pod()
    if not pod:
        return
    
    namespace = pod.metadata.namespace
    pod_name = pod.metadata.name
    
    # Obtener descripción del problema del estado del pod
    problema_desc = pod.status.phase or "Error"
    if pod.status.containerStatuses:
        for cs in pod.status.containerStatuses:
            if cs.state and cs.state.waiting and cs.state.waiting.reason:
                problema_desc = cs.state.waiting.reason
                break
            elif cs.state and cs.state.terminated and cs.state.terminated.reason:
                problema_desc = cs.state.terminated.reason
                break
    
    logger.info(f"Analizando pod {namespace}/{pod_name} ({problema_desc}) con HolmesGPT")
    
    # Recolectar contexto
    contexto = _obtener_contexto_pod(event, pod)
    
    # Llamar a HolmesGPT
    try:
        payload = {
            "source": "robusta",
            "title": f"{problema_desc}: {namespace}/{pod_name}",
            "description": f"Analiza este problema de Kubernetes y proporciona causa raíz y solución. IMPORTANTE: Responde en español de forma concisa (máximo 300 palabras).",
            "subject": {
                "name": pod_name,
                "namespace": namespace,
                "kind": "Pod"
            },
            "context": contexto
        }
        
        response = requests.post(
            f"{HOLMESGPT_URL}/api/investigate",
            json=payload,
            timeout=60,
            headers={"Content-Type": "application/json"}
        )
        
        response.raise_for_status()
        resultado = response.json()
        analisis = resultado.get("analysis", str(resultado))
        
    except Exception as e:
        logger.error(f"Error comunicándose con HolmesGPT: {e}")
        analisis = f"❌ No se pudo obtener análisis de HolmesGPT: {str(e)}"
    
    # Crear Finding conciso
    finding = Finding(
        title=f"🤖 {problema_desc}: {namespace}/{pod_name}",
        aggregation_key=f"HolmesGPT_{namespace}_{pod_name}",
        severity=FindingSeverity.HIGH,
        source=event.get_source(),
        finding_type=FindingType.ISSUE,
    )
    
    # Agregar solo el análisis de HolmesGPT
    finding.add_enrichment([
        MarkdownBlock(f"### 🔍 Diagnóstico HolmesGPT\n\n{analisis}")
    ])
    
    event.add_finding(finding)
