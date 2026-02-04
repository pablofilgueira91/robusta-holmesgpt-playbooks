# Robusta HolmesGPT Playbooks

Custom Robusta playbooks for integrating HolmesGPT AI analysis with Kubernetes pod issues.

## Features

- Automatic analysis of ImagePullBackOff errors
- CrashLoopBackOff root cause detection
- OOMKilled diagnostics

## Installation

Add to your Robusta values.yaml:

```yaml
playbookRepos:
  holmesgpt:
    url: "https://github.com/TU_USUARIO/robusta-holmesgpt-playbooks.git"
```

## Actions

- `analyze_with_holmesgpt` - Generic HolmesGPT analysis for pod issues
- `analyze_image_pull_backoff_with_holmes` - Specific ImagePullBackOff analysis
