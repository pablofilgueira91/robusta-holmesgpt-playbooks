"""
Playbook Robusta para integración con HolmesGPT
Analiza cualquier problema de Kubernetes (pods, nodes, deployments, etc.) usando IA
"""
import logging
import requests
from robusta.api import (
    action,
    ExecutionBaseEvent,
    Finding,
    FindingType,
    FindingSeverity,
    MarkdownBlock,
)

logger = logging.getLogger(__name__)

HOLMESGPT_URL = "http://holmesgpt-holmes.holmesgpt.svc.cluster.local:80"


def _limpiar_formato_slack(texto: str) -> str:
    """Limpia el análisis de HolmesGPT para mejor visualización en Slack"""
    import re
    
    # Remover secciones de External Links
    texto = re.sub(r'# External [Ll]inks.*$', '', texto, flags=re.DOTALL)
    texto = re.sub(r'## External [Ll]inks.*$', '', texto, flags=re.DOTALL)
    
    # Remover links markdown pero conservar el texto
    texto = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', texto)
    
    # Convertir headers markdown a formato Slack más legible
    texto = re.sub(r'^# (.+)$', r'*\1*', texto, flags=re.MULTILINE)
    texto = re.sub(r'^## (.+)$', r'*\1*', texto, flags=re.MULTILINE)
    texto = re.sub(r'^### (.+)$', r'*\1*', texto, flags=re.MULTILINE)
    
    # Remover bloques de código bash/shell para acortar
    texto = re.sub(r'```bash.*?```', '', texto, flags=re.DOTALL)
    texto = re.sub(r'```shell.*?```', '', texto, flags=re.DOTALL)
    texto = re.sub(r'```.*?```', '', texto, flags=re.DOTALL)
    
    # Limpiar líneas vacías múltiples
    texto = re.sub(r'\n{3,}', '\n\n', texto)
    
    # Limitar longitud si sigue siendo muy largo
    if len(texto) > 1000:
        texto = texto[:1000] + "..."
    
    return texto.strip()


def _obtener_contexto_recurso(event: ExecutionBaseEvent) -> dict:
    """Recolecta contexto del recurso de Kubernetes para HolmesGPT"""
    contexto = {
        "tipo_evento": event.__class__.__name__,
        "cluster": getattr(event.get_context(), 'cluster_name', 'unknown') if hasattr(event, 'get_context') else 'unknown'
    }
    
    # Intentar obtener información específica del recurso
    try:
        # Para recursos con método get_resource (pods, deployments, etc.)
        if hasattr(event, 'get_resource'):
            resource = event.get_resource()
            if resource:
                contexto["recurso"] = {
                    "kind": resource.kind if hasattr(resource, 'kind') else 'Unknown',
                    "nombre": resource.metadata.name if hasattr(resource, 'metadata') else 'unknown',
                    "namespace": getattr(resource.metadata, 'namespace', 'cluster-scoped') if hasattr(resource, 'metadata') else None,
                }
                
                # Estado del recurso si está disponible
                if hasattr(resource, 'status'):
                    contexto["estado"] = str(resource.status)[:500]  # Limitar tamaño
        
        # Para PodEvents específicamente
        if hasattr(event, 'get_pod'):
            pod = event.get_pod()
            if pod:
                # Eventos del pod
                if hasattr(event, 'list_pod_events'):
                    try:
                        pod_events = event.list_pod_events()
                        contexto["eventos"] = [
                            {"tipo": e.type, "razon": e.reason, "mensaje": e.message} 
                            for e in pod_events[:5]
                        ]
                    except:
                        pass
                
                # Logs del pod
                if hasattr(event, 'get_pod_logs'):
                    try:
                        logs = event.get_pod_logs()
                        contexto["logs"] = logs[-1000:] if len(logs) > 1000 else logs
                    except:
                        pass
                
                # Estado de contenedores
                if pod.status and pod.status.containerStatuses:
                    contenedores = []
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
                    contexto["contenedores"] = contenedores
        
        # Para NodeEvents
        if hasattr(event, 'get_node'):
            node = event.get_node()
            if node and node.status:
                contexto["node_conditions"] = [
                    {"tipo": c.type, "status": c.status, "razon": c.reason or "", "mensaje": c.message or ""}
                    for c in (node.status.conditions or [])
                ]
                
    except Exception as e:
        logger.warning(f"Error recolectando contexto: {e}")
    
    return contexto


@action
def analyze_with_holmesgpt(event: ExecutionBaseEvent):
    """
    Analiza CUALQUIER problema de Kubernetes usando HolmesGPT AI.
    Funciona con pods, nodes, deployments, jobs, services, etc.
    Solicita diagnóstico en español con causa raíz y solución.
    """
    # Obtener información básica del recurso
    resource_info = "Unknown"
    resource_name = "unknown"
    namespace = None
    
    try:
        if hasattr(event, 'get_resource'):
            resource = event.get_resource()
            if resource and hasattr(resource, 'metadata'):
                resource_name = resource.metadata.name
                namespace = getattr(resource.metadata, 'namespace', None)
                resource_kind = resource.kind if hasattr(resource, 'kind') else event.__class__.__name__.replace('Event', '')
                resource_info = f"{resource_kind}/{resource_name}"
        elif hasattr(event, 'get_pod'):
            pod = event.get_pod()
            if pod:
                resource_name = pod.metadata.name
                namespace = pod.metadata.namespace
                resource_info = f"Pod/{resource_name}"
        elif hasattr(event, 'get_node'):
            node = event.get_node()
            if node:
                resource_name = node.metadata.name
                resource_info = f"Node/{resource_name}"
    except:
        pass
    
    logger.info(f"Analizando {resource_info} con HolmesGPT")
    
    # Recolectar contexto
    contexto = _obtener_contexto_recurso(event)
    
    # Llamar a HolmesGPT
    try:
        payload = {
            "source": "robusta",
            "title": f"Problema en {resource_info}",
            "description": f"Analiza este problema de Kubernetes. IMPORTANTE: Responde en español de forma MUY concisa (máximo 150 palabras). Incluye SOLO: 1) Causa raíz 2) Solución específica. NO incluyas external links ni secciones largas.",
            "subject": {
                "name": resource_name,
                "namespace": namespace or "cluster-scoped",
                "kind": resource_info.split('/')[0] if '/' in resource_info else "Resource"
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
        analisis_raw = resultado.get("analysis", str(resultado))
        
        # Limpiar formato para Slack
        analisis = _limpiar_formato_slack(analisis_raw)
        
        # Extraer información de costos si está disponible
        metadata = resultado.get("metadata", {})
        usage = metadata.get("usage", {})
        
        if usage:
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            total_tokens = usage.get("total_tokens", 0)
            
            # Estimación de costo (aproximado para GPT-4, ajustar según modelo)
            costo_prompt = (prompt_tokens / 1000) * 0.03
            costo_completion = (completion_tokens / 1000) * 0.06
            costo_total = costo_prompt + costo_completion
            
            analisis += f"\n\n💰 *{total_tokens:,} tokens • ~${costo_total:.4f} USD*"
        
    except Exception as e:
        logger.error(f"Error comunicándose con HolmesGPT: {e}")
        analisis = f"❌ No se pudo obtener análisis de HolmesGPT: {str(e)}"
    
    # Crear Finding conciso
    finding = Finding(
        title=f"🤖 Análisis HolmesGPT: {resource_info}",
        aggregation_key=f"HolmesGPT_{namespace}_{resource_name}" if namespace else f"HolmesGPT_{resource_name}",
        severity=FindingSeverity.HIGH,
        source=event.get_source(),
        finding_type=FindingType.ISSUE,
    )
    
    # Agregar solo el análisis de HolmesGPT limpio y formateado
    finding.add_enrichment([
        MarkdownBlock(f"🔍 *Diagnóstico HolmesGPT*\n\n{analisis}")
    ])
    
    event.add_finding(finding)
