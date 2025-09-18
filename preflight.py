from app.wallet import preflight
try:
    preflight()
    print("Preflight OK")
except SystemExit as e:
    print(e); raise
