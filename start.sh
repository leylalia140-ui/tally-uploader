#!/bin/bash
# Production start script
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2
