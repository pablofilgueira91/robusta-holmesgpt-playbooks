"""
Custom Robusta playbook for HolmesGPT integration
This playbook sends pod issues to HolmesGPT for root cause analysis
"""
import json
import logging
import requests
from typing import Dict, Any
from hikaru.model.rel_1_26 import Pod
from robusta.api import (
    action,
    PodEvent,
    Finding,
    FindingType,
    FindingSeverity,
    MarkdownBlock,
    TableBlock,
    FileBlock,
)

logger = logging.getLogger(__name__)


@action
def analyze_with_holmesgpt(event: PodEvent):
    """
    Envía información del pod problemático a HolmesGPT para análisis de causa raíz
    
    Args:
        event: Evento de Robusta que contiene información del pod
    """
    pod: Pod = event.get_pod()
    if not pod:
        logger.error("No se pudo obtener información del pod")
        return

    # Recolectar contexto del pod
    pod_name = pod.metadata.name
    namespace = pod.metadata.namespace
    
    logger.info(f"Analizando pod {namespace}/{pod_name} con HolmesGPT")
    
    # Obtener logs del pod
    try:
        pod_logs = event.get_pod_logs()
    except Exception as e:
        logger.warning(f"No se pudieron obtener logs del pod: {e}")
        pod_logs = "No hay logs disponibles"
    
    # Obtener eventos del pod
    try:
        pod_events = event.list_pod_events()
        events_text = "\n".join([
            f"[{e.type}] {e.reason}: {e.message}"
            for e in pod_events
        ])
    except Exception as e:
        logger.warning(f"No se pudieron obtener eventos del pod: {e}")
        events_text = "No hay eventos disponibles"
    
    # Obtener estado del pod
    pod_status = {
        "phase": pod.status.phase,
        "conditions": [
            {
                "type": c.type,
                "status": c.status,
                "reason": c.reason if c.reason else "",
                "message": c.message if c.message else ""
            }
            for c in (pod.status.conditions or [])
        ],
        "containerStatuses": []
    }
    
    # Información de contenedores
    if pod.status.containerStatuses:
        for container_status in pod.status.containerStatuses:
            status_info = {
                "name": container_status.name,
                "ready": container_status.ready,
                "restartCount": container_status.restartCount,
                "image": container_status.image,
            }
            
            # Estado del contenedor
            if container_status.state:
                if container_status.state.waiting:
                    status_info["state"] = {
                        "waiting": {
                            "reason": container_status.state.waiting.reason,
                            "message": container_status.state.waiting.message or ""
                        }
                    }
                elif container_status.state.terminated:
                    status_info["state"] = {
                        "terminated": {
                            "reason": container_status.state.terminated.reason,
                            "exitCode": container_status.state.terminated.exitCode,
                            "message": container_status.state.terminated.message or ""
                        }
                    }
                elif container_status.state.running:
                    status_info["state"] = {"running": True}
            
            pod_status["containerStatuses"].append(status_info)
    
    # Construir el prompt para HolmesGPT
    context = f"""
Pod con problemas detectado en el cluster:
- Namespace: {namespace}
- Pod: {pod_name}
- Fase: {pod.status.phase}

Estado del Pod:
{json.dumps(pod_status, indent=2)}

Eventos del Pod:
{events_text}

Logs del Pod (últimas líneas):
{pod_logs[-2000:] if len(pod_logs) > 2000 else pod_logs}
"""
    
    # Llamar a HolmesGPT
    holmesgpt_url = "http://holmesgpt-holmes.holmesgpt.svc.cluster.local:80"
    
    try:
        # Preparar payload para HolmesGPT
        # HolmesGPT espera un formato específico de solicitud
        payload = {
            "context": context,
            "resource_type": "pod",
            "resource_name": pod_name,
            "namespace": namespace,
            "ask": f"Analiza el siguiente problema de pod en Kubernetes y proporciona una causa raíz y solución recomendada."
        }
        
        logger.info(f"Enviando solicitud a HolmesGPT: {holmesgpt_url}")
        
        response = requests.post(
            f"{holmesgpt_url}/api/ask",
            json=payload,
            timeout=60,
            headers={"Content-Type": "application/json"}
        )
        
        response.raise_for_status()
        holmes_analysis = response.json()
        
        # Extraer el análisis
        if isinstance(holmes_analysis, dict):
            analysis_text = holmes_analysis.get("answer") or holmes_analysis.get("analysis") or str(holmes_analysis)
        else:
            analysis_text = str(holmes_analysis)
        
        logger.info(f"Análisis recibido de HolmesGPT")
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Error al comunicarse con HolmesGPT: {e}")
        analysis_text = f"❌ Error al comunicarse con HolmesGPT: {str(e)}\n\nContexto recolectado:\n{context[:1000]}..."
    except Exception as e:
        logger.error(f"Error inesperado: {e}")
        analysis_text = f"❌ Error inesperado: {str(e)}"
    
    # Crear Finding con el análisis de HolmesGPT
    finding = Finding(
        title=f"🔍 Análisis HolmesGPT: {namespace}/{pod_name}",
        aggregation_key=f"HolmesGPT_{namespace}_{pod_name}",
        severity=FindingSeverity.HIGH,
        source=event.get_source(),
        finding_type=FindingType.ISSUE,
    )
    
    # Agregar resumen del problema
    finding.add_markdown_block(
        MarkdownBlock(
            f"**Pod problemático:** `{namespace}/{pod_name}`\n"
            f"**Fase:** {pod.status.phase}\n"
            f"**Cluster:** {event.get_context().cluster_name or 'N/A'}"
        )
    )
    
    # Agregar análisis de HolmesGPT
    finding.add_markdown_block(
        MarkdownBlock(
            f"## 🤖 Análisis de HolmesGPT\n\n{analysis_text}"
        )
    )
    
    # Agregar eventos recientes como tabla
    if pod_events and len(pod_events) > 0:
        events_data = [
            [e.type, e.reason, e.message[:100]]
            for e in pod_events[:5]  # Últimos 5 eventos
        ]
        finding.add_enrichment([
            TableBlock(
                rows=events_data,
                headers=["Tipo", "Razón", "Mensaje"],
                table_name="Eventos Recientes del Pod"
            )
        ])
    
    # Agregar logs como archivo adjunto (opcional)
    if pod_logs and len(pod_logs) > 100:
        finding.add_enrichment([
            FileBlock(
                filename=f"{pod_name}_logs.txt",
                contents=pod_logs.encode()
            )
        ])
    
    event.add_finding(finding)


@action
def analyze_image_pull_backoff_with_holmes(event: PodEvent):
    """
    Acción específica para ImagePullBackOff que envía a HolmesGPT
    """
    pod: Pod = event.get_pod()
    if not pod:
        return
    
    logger.info(f"ImagePullBackOff detectado en {pod.metadata.namespace}/{pod.metadata.name}")
    
    # Obtener información específica de ImagePullBackOff
    container_statuses = []
    if pod.status.containerStatuses:
        for cs in pod.status.containerStatuses:
            if cs.state and cs.state.waiting:
                if "ImagePullBackOff" in cs.state.waiting.reason or "ErrImagePull" in cs.state.waiting.reason:
                    container_statuses.append({
                        "name": cs.name,
                        "image": cs.image,
                        "reason": cs.state.waiting.reason,
                        "message": cs.state.waiting.message or ""
                    })
    
    # Construir contexto específico para ImagePullBackOff
    context = f"""
Problema de ImagePullBackOff detectado:
- Namespace: {pod.metadata.namespace}
- Pod: {pod.metadata.name}
- Contenedores afectados:
{json.dumps(container_statuses, indent=2)}

Por favor analiza por qué la imagen no se puede descargar y proporciona soluciones posibles.
Considera:
1. Si la imagen existe en el registro
2. Si las credenciales son correctas
3. Si el nombre de la imagen está bien escrito
4. Si hay problemas de red o permisos
"""
    
    holmesgpt_url = "http://holmesgpt-holmes.holmesgpt.svc.cluster.local:80"
    
    try:
        payload = {
            "context": context,
            "resource_type": "pod",
            "resource_name": pod.metadata.name,
            "namespace": pod.metadata.namespace,
            "ask": "Analiza este problema de ImagePullBackOff y proporciona la causa raíz y solución."
        }
        
        response = requests.post(
            f"{holmesgpt_url}/api/ask",
            json=payload,
            timeout=60,
            headers={"Content-Type": "application/json"}
        )
        
        response.raise_for_status()
        holmes_analysis = response.json()
        
        if isinstance(holmes_analysis, dict):
            analysis_text = holmes_analysis.get("answer") or holmes_analysis.get("analysis") or str(holmes_analysis)
        else:
            analysis_text = str(holmes_analysis)
        
    except Exception as e:
        logger.error(f"Error al comunicarse con HolmesGPT: {e}")
        analysis_text = f"❌ Error al comunicarse con HolmesGPT: {str(e)}\n\nContexto:\n{context}"
    
    # Crear Finding
    finding = Finding(
        title=f"🔍 ImagePullBackOff - HolmesGPT: {pod.metadata.namespace}/{pod.metadata.name}",
        aggregation_key=f"HolmesGPT_ImagePull_{pod.metadata.namespace}_{pod.metadata.name}",
        severity=FindingSeverity.HIGH,
        source=event.get_source(),
        finding_type=FindingType.ISSUE,
    )
    
    # Información del problema
    finding.add_markdown_block(
        MarkdownBlock(
            f"**🚨 Problema:** ImagePullBackOff\n"
            f"**📦 Pod:** `{pod.metadata.namespace}/{pod.metadata.name}`\n"
            f"**🖼️ Imágenes afectadas:**\n" +
            "\n".join([f"- `{cs['image']}` (contenedor: {cs['name']})" for cs in container_statuses])
        )
    )
    
    # Análisis de HolmesGPT
    finding.add_markdown_block(
        MarkdownBlock(
            f"## 🤖 Diagnóstico de HolmesGPT\n\n{analysis_text}"
        )
    )
    
    # Tabla con detalles de contenedores
    if container_statuses:
        container_data = [
            [cs['name'], cs['image'], cs['reason'], cs['message'][:50]]
            for cs in container_statuses
        ]
        finding.add_enrichment([
            TableBlock(
                rows=container_data,
                headers=["Contenedor", "Imagen", "Razón", "Mensaje"],
                table_name="Contenedores Afectados"
            )
        ])
    
    event.add_finding(finding)
