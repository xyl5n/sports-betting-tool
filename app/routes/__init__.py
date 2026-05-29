"""Flask route handlers extracted from app.py.

Each module exposes a register(app, ...) function that wires its routes
onto the shared Flask app, receiving any app-level dependencies it needs
as keyword arguments (so these modules never import app.py and stay free
of circular-import risk).
"""
