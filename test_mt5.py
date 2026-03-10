import os
from dotenv import load_dotenv
import MetaTrader5 as mt5

load_dotenv()

login = int(os.getenv("MT5_LOGIN"))
password = os.getenv("MT5_PASSWORD")
server = os.getenv("MT5_SERVER")

print(f"Connecting to {server} with login {login}...")

if not mt5.initialize():
    print(f"initialize() failed: {mt5.last_error()}")
    quit()

if not mt5.login(login, password=password, server=server):
    print(f"login() failed: {mt5.last_error()}")
    mt5.shutdown()
    quit()

info = mt5.terminal_info()
if info:
    print(f"Connected! Trade allowed: {info.trade_allowed}")
    print(f"Terminal: {info.name} build {info.build}")

acct = mt5.account_info()
if acct:
    print(f"Account: {acct.login} | Balance: {acct.balance} | Leverage: 1:{acct.leverage}")
    print(f"Server: {acct.server} | Currency: {acct.currency}")

mt5.shutdown()
print("Done.")
