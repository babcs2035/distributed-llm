"""
tools package: Operations management tools for distributed LLM inference clusters.

Each tool can be run as an independent script:
  - common.py:             Common utilities (SSH, Rsync, logging, config management)
  - deploy.py:             Auto-deploy (local build + model split + distribution + startup)
  - setup_registry.py:     Private Docker registry on management node
  - healthcheck.py:        Cluster health check
  - cluster_control.py:    Container control (stop, restart, cleanup)
  - show_logs.py:          Container log display
  - debug_tools.py:        Debug tools
  - split_model.py:        Split HF model by layer
"""
