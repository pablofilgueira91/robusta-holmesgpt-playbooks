from setuptools import setup, find_packages

setup(
    name="robusta-holmesgpt-playbooks",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "requests>=2.31.0",
    ],
    author="Pablo Filgueira",
    description="Custom Robusta playbooks for HolmesGPT integration",
    python_requires=">=3.9",
)
