import asyncio
from dataclasses import asdict
import json
import os
import random
import re
import sqlite3
from threading import Thread
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from flask import Flask
from mcd_api import MCD, DecodedOrder, Product, TokenSet, MCDAuthError, MCDError
from paypay_api import PayPay, PayPayError, PayPayLoginError

# =========================================================
# Renderのポートエラー対策 (Webサーバー設定)
# =========================================================
app = Flask("")


@app.route("/")
def home():
  return "Bot is alive!"


def run():
  # Renderが割り当てるPORT番号を取得（デフォルトは8080）
  port = int(os.environ.get("PORT", 8080))
  app.run(host="0.0.0.0", port=port)


def keep_alive():
  t = Thread(target=run)
  t.start()


# =========================================================
# メイン処理
# =========================================================
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROXY_FILE = os.path.join(BASE_DIR, "Proxy", "proxy.json")
MC_DB_PATH = os.path.join(BASE_DIR, "mac.db")
PAY_DB_PATH = os.path.join(BASE_DIR, "pay.db")


class MCDBManager:

  def __init__(self, db_path: str):
    self.db_path = db_path
    self._init_db()

  def _init_db(self):
    with sqlite3.connect(self.db_path) as conn:
      conn.execute(
          "CREATE TABLE IF NOT EXISTS users (user_id TEXT PRIMARY KEY, tokens"
          " TEXT, email TEXT)"
      )
      conn.commit()

  def save_user(self, user_id: str, email: str, tokens: TokenSet):
    tokens_json = json.dumps(asdict(tokens))
    with sqlite3.connect(self.db_path) as conn:
      conn.execute(
          "INSERT OR REPLACE INTO users (user_id, email, tokens) VALUES (?,"
          " ?, ?)",
          (user_id, email, tokens_json),
      )
      conn.commit()

  def get_user_tokens(self, user_id: str) -> Optional[TokenSet]:
    with sqlite3.connect(self.db_path) as conn:
      row = conn.execute(
          "SELECT tokens FROM users WHERE user_id = ?", (user_id,)
      ).fetchone()
      if row:
        return TokenSet(**json.loads(row[0]))
    return None

  def delete_user(self, user_id: str) -> bool:
    with sqlite3.connect(self.db_path) as conn:
      cursor = conn.execute(
          "DELETE FROM users WHERE user_id = ?", (user_id,)
      )
      conn.commit()
      return cursor.rowcount > 0


class PayDBManager:

  def __init__(self, db_path: str):
    self.db_path = db_path
    self._init_db()

  def _init_db(self):
    with sqlite3.connect(self.db_path) as conn:
      conn.execute("""
                CREATE TABLE IF NOT EXISTS pay_users (
                    user_id TEXT PRIMARY KEY, phone TEXT, access_token TEXT, 
                    refresh_token TEXT, device_uuid TEXT, client_uuid TEXT
                )
            """)
      conn.commit()

  def save_pay_user(
      self,
      user_id: str,
      phone: str,
      access_token: str,
      refresh_token: str = "",
      device_uuid: str = "",
      client_uuid: str = "",
  ):
    with sqlite3.connect(self.db_path) as conn:
      conn.execute(
          "INSERT OR REPLACE INTO pay_users (user_id, phone, access_token,"
          " refresh_token, device_uuid, client_uuid) VALUES (?, ?, ?, ?, ?,"
          " ?)",
          (
              user_id,
              phone,
              access_token,
              refresh_token,
              device_uuid,
              client_uuid,
          ),
      )
      conn.commit()

  def get_pay_tokens(self, user_id: str) -> Optional[dict]:
    with sqlite3.connect(self.db_path) as conn:
      conn.row_factory = sqlite3.Row
      row = conn.execute(
          "SELECT * FROM pay_users WHERE user_id = ?", (user_id,)
      ).fetchone()
      return dict(row) if row else None

  def delete_pay_user(self, user_id: str) -> bool:
    with sqlite3.connect(self.db_path) as conn:
      cursor = conn.execute(
          "DELETE FROM pay_users WHERE user_id = ?", (user_id,)
      )
      conn.commit()
      return cursor.rowcount > 0


mc_db = MCDBManager(MC_DB_PATH)
pay_db = PayDBManager(PAY_DB_PATH)


def get_random_proxy():
  try:
    if os.path.exists(PROXY_FILE):
      with open(PROXY_FILE, "r") as f:
        data = json.load(f)
        proxies = data.get("proxy", [])
        if proxies:
          return random.choice(proxies)
  except Exception:
    pass
  return None


def extract_store_id(input_str: str) -> str:
  match = re.search(r"(\d{5})", input_str)
  return match.group(1) if match else input_str.strip()


def get_order_details(
    store_data: dict, product_ids: list[str]
) -> tuple[int, list[str]]:
  total = 0
  product_names = []
  menu_products = store_data.get("menu", {}).get("products", [])
  menu_map = {
      str(p.get("id")): (
          p.get("price", {}).get("amount", 0),
          p.get("name", "不明な商品"),
      )
      for p in menu_products
  }
  for pid in product_ids:
    info = menu_map.get(pid)
    if info:
      price, name = info
      total += int(price)
      product_names.append(name)
    else:
      product_names.append(f"不明な商品(ID:{pid})")
  if not any(pid in menu_map for pid in product_ids):
    raise ValueError("指定された商品IDがメニューに見つかりませんでした。")
  return total, product_names


async def process_paypay_payment(
    user_id: str,
    paypay_url: str,
    passcode: str,
    total_amount: int,
    proxy: str = None,
):
  pay_info = pay_db.get_pay_tokens(user_id)
  if not pay_info:
    raise Exception("PayPayにログインしていません。")
  pp = PayPay(
      access_token=pay_info["access_token"],
      device_uuid=pay_info["device_uuid"],
      client_uuid=pay_info["client_uuid"],
      proxy=proxy,
  )
  try:
    link_info = pp.link_check(paypay_url)
    if link_info.status != "PENDING":
      raise Exception(f"リンクは既に処理されています ({link_info.status})")
    target_amount = total_amount // 2
    if link_info.amount != target_amount:
      raise Exception(
          f"PayPay金額(¥{link_info.amount})が支払額(¥{target_amount})と一致しません。"
      )
    pp.link_receive(paypay_url, passcode, link_info)
    return True
  except PayPayError as e:
    raise Exception(f"PayPayエラー: {str(e)}")


class PayPayModal(discord.ui.Modal, title="PayPay 支払い確定"):
  paypay_url = discord.ui.TextInput(
      label="PayPay URL",
      placeholder="https://pay.paypay.ne.jp/...",
      required=True,
  )
  passcode = discord.ui.TextInput(
      label="パスコード",
      placeholder="4桁の数字",
      required=True,
      min_length=4,
      max_length=4,
  )

  def __init__(self, user_id, mcd_instance, decoded_order, total_amount, proxy):
    super().__init__()
    (
        self.user_id,
        self.mcd,
        self.decoded,
        self.total_amount,
        self.proxy,
    ) = (user_id, mcd_instance, decoded_order, total_amount, proxy)

  async def on_submit(self, interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
      await process_paypay_payment(
          str(self.user_id),
          self.paypay_url.value,
          self.passcode.value,
          self.total_amount,
          self.proxy,
      )
      order_res = self.mcd.store_order(self.decoded)
      result = self.mcd.pay_from_hex(order_res.raw_hex)
      embed = discord.Embed(title="注文完了", color=discord.Color.green())
      embed.add_field(name="店舗名", value=result.store_name, inline=False)
      embed.add_field(
          name="合計金額", value=f"¥{self.total_amount}", inline=True
      )
      embed.add_field(
          name="注文番号", value=result.receipt_number, inline=True
      )
      await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
      await interaction.followup.send(f"エラー: {str(e)}", ephemeral=True)


class ConfirmOrderView(discord.ui.View):

  def __init__(self, user_id, mcd_instance, decoded_order, total_amount, proxy):
    super().__init__(timeout=180)
    (
        self.user_id,
        self.mcd,
        self.decoded,
        self.total_amount,
        self.proxy,
    ) = (user_id, mcd_instance, decoded_order, total_amount, proxy)

  @discord.ui.button(label="支払い確定", style=discord.ButtonStyle.danger)
  async def confirm(
      self, interaction: discord.Interaction, button: discord.ui.Button
  ):
    await interaction.response.send_modal(
        PayPayModal(
            self.user_id,
            self.mcd,
            self.decoded,
            self.total_amount,
            self.proxy,
        )
    )

  @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
  async def cancel(
      self, interaction: discord.Interaction, button: discord.ui.Button
  ):
    await interaction.response.send_message(
        "キャンセルしました。", ephemeral=True
    )
    self.stop()


class HexGenModal(discord.ui.Modal, title="店舗ID/商品IDから注文"):
  store_input = discord.ui.TextInput(
      label="店舗ID または 店舗URL", placeholder="例: 08629", required=True
  )
  product_input = discord.ui.TextInput(
      label="商品ID (カンマ区切り)", placeholder="例: 2254, 2255", required=True
  )

  async def on_submit(self, interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    tokens = mc_db.get_user_tokens(str(interaction.user.id))
    if not tokens:
      return await interaction.followup.send(
          "マックにログインしてください。", ephemeral=True
      )
    if not pay_db.get_pay_tokens(str(interaction.user.id)):
      return await interaction.followup.send(
          "PayPayにログインしてください。", ephemeral=True
      )
    store_id = extract_store_id(self.store_input.value)
    product_ids = [
        p.strip() for p in self.product_input.value.split(",") if p.strip()
    ]
    proxy = get_random_proxy()
    mcd = MCD(tokens=tokens, proxy=proxy)
    try:
      store_data = mcd.get_store(store_id)
      if not store_data:
        return await interaction.followup.send(
            "店舗情報が取得できません。", ephemeral=True
        )
      store_name = store_data.get("store", {}).get("name", "不明な店舗")
      total_amount, product_names = get_order_details(store_data, product_ids)
      decoded = DecodedOrder(
          store_id=store_id,
          short_order_code="",
          amount_cents=total_amount,
          products=[Product(product_id=pid) for pid in product_ids],
          pickup_method="テイクアウト",
      )
      embed = discord.Embed(
          title="注文内容の確認", color=discord.Color.orange()
      )
      embed.add_field(name="店舗名", value=store_name, inline=False)
      embed.add_field(
          name="注文商品",
          value="\n".join([f"・{name}" for name in product_names]),
          inline=False,
      )
      embed.add_field(
          name="合計金額", value=f"¥{total_amount}", inline=False
      )
      await interaction.followup.send(
          embed=embed,
          view=ConfirmOrderView(
              interaction.user.id, mcd, decoded, total_amount, proxy
          ),
          ephemeral=True,
      )
    except Exception as e:
      await interaction.followup.send(f"エラー: {str(e)}", ephemeral=True)


class ControlPanelView(discord.ui.View):

  def __init__(self):
    super().__init__(timeout=None)

  @discord.ui.button(
      label="IDから注文 (自動生成)",
      style=discord.ButtonStyle.success,
      custom_id="mcd_gen_order_btn",
  )
  async def gen_order_button(
      self, interaction: discord.Interaction, button: discord.ui.Button
  ):
    await interaction.response.send_modal(HexGenModal())


class MCDBot(commands.Bot):

  def __init__(self):
    intents = discord.Intents.default()
    intents.message_content = True
    super().__init__(command_prefix="!", intents=intents)

  async def setup_hook(self):
    self.add_view(ControlPanelView())
    await self.tree.sync()


bot = MCDBot()


@bot.tree.command(
    name="mclogin", description="マックのアカウントにログインします"
)
async def mclogin(interaction: discord.Interaction, email: str, password: str):
  await interaction.response.defer(ephemeral=True)
  proxy = get_random_proxy()
  mcd = MCD(proxy=proxy)
  try:
    mfa_token = mcd.login(email, password)
    await interaction.followup.send(
        "DMを確認し、120秒以内にOTPを入力してください。", ephemeral=True
    )

    def check(m):
      return m.author == interaction.user and isinstance(
          m.channel, discord.DMChannel
      )

    msg = await bot.wait_for("message", check=check, timeout=120.0)
    tokens = mcd.login_with_mfa(mfa_token, msg.content.strip())
    mc_db.save_user(str(interaction.user.id), email, tokens)
    await interaction.user.send("マックログイン成功！")
  except Exception as e:
    await interaction.followup.send(f"エラー: {str(e)}", ephemeral=True)


@bot.tree.command(name="mclogout")
async def mclogout(interaction: discord.Interaction):
  if mc_db.delete_user(str(interaction.user.id)):
    await interaction.response.send_message("ログアウト完了。", ephemeral=True)
  else:
    await interaction.response.send_message("未ログインです。", ephemeral=True)


@bot.tree.command(
    name="paylogin", description="PayPayのアカウントにログインします"
)
async def paylogin(
    interaction: discord.Interaction, number: str, password: str
):
  await interaction.response.defer(ephemeral=True)
  proxy = get_random_proxy()
  pp = PayPay(phone=number, password=password, proxy=proxy)
  try:
    try:
      pp.login()
    except PayPayLoginError as e:

      if "OTP" not in str(e):
        return await interaction.followup.send(
            f"ログインエラー: {str(e)}", ephemeral=True
        )

    await interaction.followup.send(
        "DMを確認し、SMSに届いたURLを120秒以内に送信してください。",
        ephemeral=True,
    )

    def check(m):
      return m.author == interaction.user and isinstance(
          m.channel, discord.DMChannel
      )

    try:
      msg = await bot.wait_for("message", check=check, timeout=120.0)
      otp_input = msg.content.strip()

      token_res = pp.verify_otp(otp_input)

      pay_db.save_pay_user(
          user_id=str(interaction.user.id),
          phone=number,
          access_token=pp.access_token,
          refresh_token=pp.refresh_token,
          device_uuid=pp.device_uuid,
          client_uuid=pp.client_uuid,
      )
      await interaction.user.send("PayPayログイン成功！")

    except asyncio.TimeoutError:
      await interaction.user.send(
          "タイムアウトしました。もう一度やり直してください。"
      )
    except Exception as e:
      await interaction.user.send(f"OTP検証エラー: {str(e)}")

  except Exception as e:
    await interaction.followup.send(f"エラー: {str(e)}", ephemeral=True)


@bot.tree.command(name="paylogout")
async def paylogout(interaction: discord.Interaction):
  if pay_db.delete_pay_user(str(interaction.user.id)):
    await interaction.response.send_message("ログアウト完了。", ephemeral=True)
  else:
    await interaction.response.send_message("未ログインです。", ephemeral=True)


@bot.tree.command(name="panel")
async def panel(interaction: discord.Interaction):
  embed = discord.Embed(
      title="McDonald's Mobile Order Panel",
      description="下のボタンから注文を開始してください。",
      color=discord.Color.red(),
  )
  await interaction.response.send_message(embed=embed, view=ControlPanelView())


if __name__ == "__main__":
  # Webサーバーをバックグラウンドで起動
  keep_alive()

  # Botの起動
  bot.run(TOKEN)
