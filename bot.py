import discord
import os
import requests
import asyncio
from discord.ext import commands
from dotenv import load_dotenv
import logging
from google.oauth2 import service_account
from googleapiclient.discovery import build
from datetime import datetime, timedelta
from discord import app_commands

# Suppress the oauth2client warning
logging.getLogger('googleapiclient.discovery_cache').setLevel(logging.ERROR)

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID")
GUILD_ID = os.getenv("GUILD_ID")
GOOGLE_SHEETS_CREDENTIALS = os.getenv("GOOGLE_SHEETS_CREDENTIALS")

# Debug: Print env vars to logs
print(f"DISCORD_BOT_TOKEN: {DISCORD_BOT_TOKEN}")
print(f"GOOGLE_SHEETS_ID: {GOOGLE_SHEETS_ID}")
print(f"GOOGLE_SHEETS_CREDENTIALS: {GOOGLE_SHEETS_CREDENTIALS[:50] if GOOGLE_SHEETS_CREDENTIALS else None}...")
print(f"GUILD_ID: {GUILD_ID}")

if not all([DISCORD_BOT_TOKEN, GOOGLE_SHEETS_ID]):
    logger.error("Missing environment variables.")
    exit(1)

if not GUILD_ID:
    logger.warning("GUILD_ID not set in .env. Guild-specific sync will be skipped. Global sync may take up to an hour.")

# Google Sheets setup
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

# Load Google Sheets credentials from environment variable
import json
import tempfile

if GOOGLE_SHEETS_CREDENTIALS:
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json') as temp_file:
        json.dump(json.loads(GOOGLE_SHEETS_CREDENTIALS), temp_file)
        temp_file_path = temp_file.name
    creds = service_account.Credentials.from_service_account_file(temp_file_path, scopes=SCOPES)
    os.unlink(temp_file_path)  # Delete the temporary file
else:
    creds = service_account.Credentials.from_service_account_file('service-account.json', scopes=SCOPES)

sheets_service = build('sheets', 'v4', credentials=creds)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# Cached data
log_filters_cache = None
recent_logs = []

async def upload_image_to_imgbb(image_url: str) -> str:
    """Upload an image to ImgBB with retry and proper async delay."""
    api_key = "7ee1d7413ff2b101d7d3ffab0319bda0"  # Your ImgBB API key
    for attempt in range(3):
        try:
            response = requests.post("https://api.imgbb.com/1/upload", data={"key": api_key, "image": image_url}, timeout=5)
            response.raise_for_status()
            link = response.json()["data"]["url"]
            logger.info(f"Uploaded image to ImgBB: {link}")
            return link
        except requests.RequestException as e:
            logger.error(f"Attempt {attempt + 1}/3 - Error uploading image: {e}")
            if attempt < 2:
                await asyncio.sleep(2)  # Proper async delay
            else:
                logger.error("All retries failed.")
                return None

def add_to_google_sheets(creator_name: str, link: str, price: str) -> bool:
    """Add a record to Logs sheet."""
    try:
        result = sheets_service.spreadsheets().values().batchGet(
            spreadsheetId=GOOGLE_SHEETS_ID, ranges=["Logs!C:C"]).execute()
        max_rows = len(result.get('valueRanges', [{}])[0].get('values', []))
        next_row = max_rows + 1
        sheets_service.spreadsheets().values().update(
            spreadsheetId=GOOGLE_SHEETS_ID, range=f"Logs!A{next_row}:G{next_row}",
            valueInputOption="USER_ENTERED", body={"values": [["", "", creator_name, "", link, "", price]]}
        ).execute()
        return True
    except Exception as e:
        logger.error(f"Error adding to Sheets: {e}")
        return False

def remove_recent_from_sheets(count: int = 1) -> tuple[bool, list[tuple[str, str, str]]]:
    """Remove recent records from Logs sheet."""
    try:
        result = sheets_service.spreadsheets().values().batchGet(
            spreadsheetId=GOOGLE_SHEETS_ID, ranges=["Logs!C:C", "Logs!E:E", "Logs!G:G"]).execute()
        value_ranges = result.get('valueRanges', [])
        max_rows = max(len(v.get('values', [])) for v in value_ranges)

        if max_rows <= 1:
            return False, []

        rows_to_remove = min(count, max_rows - 1)
        removed_records = [
            (
                value_ranges[0].get('values', [])[max_rows - 1 - i][0] if max_rows - 1 - i < len(value_ranges[0].get('values', [])) else "",
                value_ranges[1].get('values', [])[max_rows - 1 - i][0] if max_rows - 1 - i < len(value_ranges[1].get('values', [])) else "",
                value_ranges[2].get('values', [])[max_rows - 1 - i][0] if max_rows - 1 - i < len(value_ranges[2].get('values', [])) else ""
            ) for i in range(rows_to_remove)
        ]

        for i in range(rows_to_remove):
            row = max_rows - i
            for col in ["C", "E", "G"]:
                sheets_service.spreadsheets().values().clear(
                    spreadsheetId=GOOGLE_SHEETS_ID, range=f"Logs!{col}{row}").execute()
        return True, removed_records[::-1]
    except Exception as e:
        logger.error(f"Error removing records: {e}")
        return False, []

def get_month_summary(month_abbr: str) -> tuple[bool, str, str, str, str]:
    """Fetch summary data from the specified month's sheet."""
    try:
        month_short = month_abbr.upper()
        month_full = datetime.strptime(month_short, "%b").strftime("%B")
        result = sheets_service.spreadsheets().values().batchGet(
            spreadsheetId=GOOGLE_SHEETS_ID, ranges=[f"{month_short}!Q15", f"{month_short}!Q18", f"{month_short}!Q21"]).execute()
        value_ranges = result.get('valueRanges', [])
        earned = value_ranges[0].get('values', [['$0.00']])[0][0]
        pending = value_ranges[1].get('values', [['$0.00']])[0][0]
        work_done = value_ranges[2].get('values', [['0']])[0][0]
        return True, month_full, earned, pending, work_done
    except Exception as e:
        logger.error(f"Error fetching summary: {e}")
        return False, "", "$0.00", "$0.00", "0"

def get_logs() -> tuple[bool, list[tuple[str, str, str, str]]]:
    """Fetch non-empty records from Logs sheet."""
    try:
        result = sheets_service.spreadsheets().values().batchGet(
            spreadsheetId=GOOGLE_SHEETS_ID, ranges=["Logs!C7:C", "Logs!E7:E", "Logs!G7:G", "Logs!J7:J"]).execute()
        value_ranges = result.get('valueRanges', [])
        max_rows = max(len(v.get('values', [])) for v in value_ranges)
        logs = [
            (
                value_ranges[0].get('values', [])[i][0] if i < len(value_ranges[0].get('values', [])) else "N/A",
                value_ranges[1].get('values', [])[i][0] if i < len(value_ranges[1].get('values', [])) else "N/A",
                value_ranges[2].get('values', [])[i][0] if i < len(value_ranges[2].get('values', [])) else "N/A",
                value_ranges[3].get('values', [])[i][0] if i < len(value_ranges[3].get('values', [])) else "No"
            ) for i in range(max_rows)
            if not all(x in ["N/A", "No", "", "FALSE"] for x in [
                value_ranges[0].get('values', [])[i][0] if i < len(value_ranges[0].get('values', [])) else "N/A",
                value_ranges[1].get('values', [])[i][0] if i < len(value_ranges[1].get('values', [])) else "N/A",
                value_ranges[2].get('values', [])[i][0] if i < len(value_ranges[2].get('values', [])) else "N/A",
                value_ranges[3].get('values', [])[i][0] if i < len(value_ranges[3].get('values', [])) else "No"
            ])
        ]
        return True, logs
    except Exception as e:
        logger.error(f"Error fetching logs: {e}")
        return False, []

def get_log_filters() -> list[str]:
    """Fetch and cache creator names from Logs!P7:P18."""
    global log_filters_cache
    if log_filters_cache is None:
        try:
            result = sheets_service.spreadsheets().values().get(
                spreadsheetId=GOOGLE_SHEETS_ID, range="Logs!P7:P18").execute()
            values = result.get('values', [])
            log_filters_cache = [row[0] for row in values if row and row[0]]
            logger.info(f"Cached {len(log_filters_cache)} creator names from Logs!P7:P18")
        except Exception as e:
            logger.error(f"Error fetching log filters: {e}")
            return []
    return log_filters_cache

def end_month() -> tuple[bool, str, str, str, str]:
    """End the current month and start the next."""
    try:
        current_month_short = datetime.now().strftime("%b").upper()
        current_month_full = datetime.now().strftime("%B")
        next_month_short = (datetime.now().replace(day=1) + timedelta(days=32)).strftime("%b").upper()
        next_month_full = (datetime.now().replace(day=1) + timedelta(days=32)).strftime("%B")

        spreadsheet = sheets_service.spreadsheets().get(spreadsheetId=GOOGLE_SHEETS_ID).execute()
        current_sheet_id = next((sheet['properties']['sheetId'] for sheet in spreadsheet['sheets'] if sheet['properties']['title'] == current_month_short), None)
        if not current_sheet_id:
            raise ValueError(f"Sheet '{current_month_short}' not found")

        success, _, earned, pending, work_done = get_month_summary(current_month_short)
        if not success:
            raise ValueError("Failed to fetch stats")

        q15_value = sheets_service.spreadsheets().values().get(
            spreadsheetId=GOOGLE_SHEETS_ID, range=f"{current_month_short}!Q15").execute().get('values', [['']])[0][0]
        archived_sheet_name = f"{current_month_full} ({q15_value})"

        sheets_service.spreadsheets().batchUpdate(spreadsheetId=GOOGLE_SHEETS_ID, body={
            "requests": [{"duplicateSheet": {"sourceSheetId": current_sheet_id, "insertSheetIndex": 1, "newSheetName": archived_sheet_name}}]}).execute()
        spreadsheet = sheets_service.spreadsheets().get(spreadsheetId=GOOGLE_SHEETS_ID).execute()
        archived_sheet_id = next((sheet['properties']['sheetId'] for sheet in spreadsheet['sheets'] if sheet['properties']['title'] == archived_sheet_name), None)

        values = sheets_service.spreadsheets().values().get(
            spreadsheetId=GOOGLE_SHEETS_ID, range=f"{current_month_short}!A1:Z1000").execute().get('values', [[]])
        sheets_service.spreadsheets().values().update(
            spreadsheetId=GOOGLE_SHEETS_ID, range=f"{archived_sheet_name}!A1:Z1000", valueInputOption="RAW", body={"values": values}).execute()

        sheets_service.spreadsheets().batchUpdate(spreadsheetId=GOOGLE_SHEETS_ID, body={
            "requests": [{"updateSheetProperties": {"properties": {"sheetId": archived_sheet_id, "hidden": True}, "fields": "hidden"}}]}).execute()
        sheets_service.spreadsheets().batchUpdate(spreadsheetId=GOOGLE_SHEETS_ID, body={
            "requests": [{"updateSheetProperties": {"properties": {"sheetId": current_sheet_id, "title": next_month_short}, "fields": "title"}}]}).execute()
        sheets_service.spreadsheets().values().clear(spreadsheetId=GOOGLE_SHEETS_ID, range="Logs!A7:G").execute()

        return True, current_month_full, earned, pending, work_done
    except Exception as e:
        logger.error(f"Error in end_month: {e}")
        return False, "", "$0.00", "$0.00", "0"

@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user}")
    try:
        registered_commands = [cmd.name for cmd in bot.tree.get_commands()]
        logger.info(f"Registered commands before sync: {registered_commands}")

        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            synced = await bot.tree.sync(guild=guild)
            logger.info(f"Synced {len(synced)} commands to guild {GUILD_ID}: {[cmd.name for cmd in synced]}")
        else:
            logger.warning("GUILD_ID not set, skipping guild-specific sync.")

        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} commands globally: {[cmd.name for cmd in synced]}")
    except Exception as e:
        logger.error(f"Sync failed: {e}")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    if message.attachments:
        for attachment in message.attachments:
            if attachment.filename.lower().endswith(".png"):
                img_link = await upload_image_to_imgbb(attachment.url)  # Updated to ImgBB
                if img_link:
                    embed = discord.Embed(color=discord.Color.dark_grey())
                    embed.add_field(name="ImgBB Link", value=f"`{img_link}`", inline=False)  # Updated label
                    await message.channel.send(embed=embed)
                else:
                    await message.channel.send("Failed to upload to ImgBB. Please try again.")  # Updated message
            else:
                await message.channel.send("Please send a PNG image.")

async def creator_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    creators = get_log_filters()
    return [app_commands.Choice(name=c, value=c) for c in creators if current.lower() in c.lower()][:25]

async def count_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    success, logs = get_logs()
    if not success or not logs:
        return [app_commands.Choice(name="1", value="1")]
    return [app_commands.Choice(name=str(i), value=str(i)) for i in range(1, min(len(logs) + 1, 26)) if current == "" or current in str(i)]

async def link_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    global recent_logs
    if not recent_logs:
        return []
    short_links = [log[1].split('/')[-1] for log in recent_logs]
    return [app_commands.Choice(name=link, value=link) for link in short_links if current.lower() in link.lower()][:25]

@bot.tree.command(name="sync", description="Manually sync commands (admin only).")
async def sync_slash(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You need to be an administrator to use this command.", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            synced = await bot.tree.sync(guild=guild)
            logger.info(f"Manually synced {len(synced)} commands to guild {GUILD_ID}: {[cmd.name for cmd in synced]}")
            await interaction.followup.send(f"Synced {len(synced)} commands to this guild.")
        synced = await bot.tree.sync()
        logger.info(f"Manually synced {len(synced)} commands globally: {[cmd.name for cmd in synced]}")
        await interaction.followup.send(f"Synced {len(synced)} commands globally (may take up to an hour to propagate).")
    except Exception as e:
        logger.error(f"Manual sync failed: {e}")
        await interaction.followup.send(f"Failed to sync commands: {e}")

@bot.tree.command(name="add", description="Add a record to Google Sheets.")
@app_commands.describe(creator_name="Creator name", link="Content URL", price="Price (e.g., 10.50)")
@app_commands.autocomplete(creator_name=creator_autocomplete)
async def add_slash(interaction: discord.Interaction, creator_name: str, link: str, price: str):
    await interaction.response.defer()
    try:
        float(price.replace('$', ''))
        if add_to_google_sheets(creator_name, link, price):
            embed = discord.Embed(title="Record Added", color=discord.Color.purple())
            embed.add_field(name="Creator", value=creator_name, inline=True)
            embed.add_field(name="Link", value=f"[Click Here]({link})", inline=True)
            embed.add_field(name="Price", value=f"${price}", inline=True)
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send("Failed to add record.")
    except ValueError:
        await interaction.followup.send("Invalid price format.")

@bot.tree.command(name="removerecent", description="Remove recent records.")
@app_commands.describe(count="Number of records to remove (default 1)")
@app_commands.autocomplete(count=count_autocomplete)
async def removerecent_slash(interaction: discord.Interaction, count: int = 1):
    await interaction.response.defer()
    if count < 1:
        await interaction.followup.send("Specify a positive number.")
        return
    success, removed = remove_recent_from_sheets(count)
    if success and removed:
        embed = discord.Embed(title=f"Removed {len(removed)} Record{'s' if len(removed) > 1 else ''}", color=discord.Color.red())
        for i, (name, link, price) in enumerate(removed, 1):
            embed.add_field(name=f"Record {i}", value=f"**Creator:** {name}\n**Link:** [Click Here]({link})\n**Price:** ${price}", inline=False)
        await interaction.followup.send(embed=embed)
    elif success:
        await interaction.followup.send("No records to remove.")
    else:
        await interaction.followup.send("Failed to remove records.")

@bot.tree.command(name="summary", description="Show current month summary.")
async def summary_slash(interaction: discord.Interaction):
    await interaction.response.defer()
    success, month, earned, pending, work_done = get_month_summary(datetime.now().strftime("%b").upper())
    if success:
        embed = discord.Embed(title=f"{month} Overview", color=discord.Color.green())
        embed.add_field(name="💰 Total Earned", value=f"**{earned}**", inline=True)
        embed.add_field(name="⏳ Total Pending", value=f"**{pending}**", inline=True)
        embed.add_field(name="✅ Total Work Done", value=f"**{work_done}**", inline=True)
        await interaction.followup.send(embed=embed)
    else:
        await interaction.followup.send(f"Failed to fetch summary for {month}.")

@bot.tree.command(name="logs", description="Display logs with filter option.")
@app_commands.describe(filter="Filter by creator name (optional)")
@app_commands.autocomplete(filter=creator_autocomplete)
async def logs_slash(interaction: discord.Interaction, filter: str = None):
    global recent_logs
    await interaction.response.defer()
    success, logs = get_logs()
    if not success:
        await interaction.followup.send("Failed to fetch logs.")
        return

    if filter:
        logs = [log for log in logs if log[0] == filter]

    if not logs:
        embed = discord.Embed(title="Logs Overview", description="All non-empty records", color=discord.Color.blue())
        await interaction.followup.send(embed=embed.add_field(name="No Records", value=f"No records{f' for {filter}' if filter else ''}.", inline=False))
        return

    recent_logs = logs
    creator_width = max(len("Creator"), max((len(log[0]) for log in logs), default=0))
    link_width = max(len("Link"), max((len(log[1].split('/')[-1]) for log in logs), default=0))
    price_width = max(len("Price"), max((len(log[2]) for log in logs), default=0))
    paid_width = max(len("Paid"), max((len("Yes" if log[3].lower() in ["yes", "true"] else "No") for log in logs), default=0))

    table_content = "```\n"
    table_content += f"{'Creator'.ljust(creator_width)} | {'Link'.ljust(link_width)} | {'Price'.ljust(price_width)} | {'Paid'.ljust(paid_width)}\n"
    table_content += f"{'-' * creator_width} | {'-' * link_width} | {'-' * price_width} | {'-' * paid_width}\n"
    for name, link, price, paid in logs:
        paid_display = "Yes" if paid.lower() in ["yes", "true"] else "No"
        short_link = link.split('/')[-1]
        table_content += f"{name.ljust(creator_width)} | {short_link.ljust(link_width)} | {price.ljust(price_width)} | {paid_display.ljust(paid_width)}\n"
    table_content += "```\n"

    embed = discord.Embed(title="Logs Overview", description="All non-empty records. Use /getlink or /getimage.", color=discord.Color.blue())
    embed.add_field(name="Entries", value=table_content, inline=False)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="getlink", description="Retrieve the full URL from the most recent /logs output.")
@app_commands.describe(link="The short link to retrieve (e.g., hv1xR5r.png)")
@app_commands.autocomplete(link=link_autocomplete)
async def getlink_slash(interaction: discord.Interaction, link: str):
    await interaction.response.defer()
    global recent_logs
    if not recent_logs:
        await interaction.followup.send("No recent /logs output found. Please run /logs first.")
        return

    full_link = next((log[1] for log in recent_logs if log[1].split('/')[-1] == link), None)
    if full_link:
        embed = discord.Embed(title="Full URL", color=discord.Color.blue())
        embed.add_field(name="Link", value=f"[Click Here]({full_link})", inline=False)
        await interaction.followup.send(embed=embed)
    else:
        await interaction.followup.send(f"Link '{link}' not found in the most recent /logs output.")

@bot.tree.command(name="getimage", description="Display the image from the most recent /logs output.")
@app_commands.describe(link="The short link to display (e.g., hv1xR5r.png)")
@app_commands.autocomplete(link=link_autocomplete)
async def getimage_slash(interaction: discord.Interaction, link: str):
    await interaction.response.defer()
    global recent_logs
    if not recent_logs:
        await interaction.followup.send("No recent /logs output found. Please run /logs first.")
        return

    full_link = next((log[1] for log in recent_logs if log[1].split('/')[-1] == link), None)
    if full_link:
        embed = discord.Embed(title="Image Display", color=discord.Color.blue())
        embed.set_image(url=full_link)
        await interaction.followup.send(embed=embed)
    else:
        await interaction.followup.send(f"Link '{link}' not found in the most recent /logs output.")

@bot.tree.command(name="endmonth", description="End current month and start next.")
async def endmonth_slash(interaction: discord.Interaction):
    await interaction.response.defer()
    success, month, earned, pending, work_done = end_month()
    if success:
        next_month = (datetime.now().replace(day=1) + timedelta(days=32)).strftime("%B")
        embed = discord.Embed(title=f"🎉 {month} Wrapped Up!", description=f"{next_month} now active!", color=discord.Color.gold())
        embed.add_field(name="💰 Total Earned", value=f"**{earned}**", inline=True)
        embed.add_field(name="⏳ Total Pending", value=f"**{pending}**", inline=True)
        embed.add_field(name="✅ Work Completed", value=f"**{work_done}**", inline=True)
        await interaction.followup.send(embed=embed)
    else:
        await interaction.followup.send("Failed to end month.")

@bot.tree.command(name="nuke", description="Erase all messages (requires confirmation).")
async def nuke_slash(interaction: discord.Interaction):
    if not interaction.channel.permissions_for(interaction.guild.me).manage_messages:
        await interaction.response.send_message("I need 'Manage Messages' permission!")
        return
    await interaction.response.send_message("Type `yes` to confirm (10s timeout).")
    try:
        await bot.wait_for('message', check=lambda m: m.author == interaction.user and m.channel == interaction.channel and m.content.lower() == "yes", timeout=10.0)
        async with interaction.channel.typing():
            deleted = await interaction.channel.purge(limit=None)
            await interaction.edit_original_response(content=f"Nuked {len(deleted)} messages!")
    except asyncio.TimeoutError:
        await interaction.edit_original_response(content="Nuke cancelled.")

@bot.tree.command(name="help", description="Display available commands.")
async def help_slash(interaction: discord.Interaction):
    embed = discord.Embed(title="Bot Commands", color=discord.Color.blue())
    embed.add_field(name="Image Upload", value="Send PNG for ImgBB link.", inline=False)  # Updated label
    embed.add_field(name="/sync", value="Manually sync commands (admin only).", inline=False)
    embed.add_field(name="/add", value="Add record to Sheets.", inline=False)
    embed.add_field(name="/removerecent", value="Remove recent records.", inline=False)
    embed.add_field(name="/summary", value="Current month summary.", inline=False)
    embed.add_field(name="/logs", value="Display logs with short links in table.", inline=False)
    embed.add_field(name="/getlink", value="Retrieve full URL from /logs output.", inline=False)
    embed.add_field(name="/getimage", value="Display the image from /logs output.", inline=False)
    embed.add_field(name="/endmonth", value="End month, start next.", inline=False)
    embed.add_field(name="/nuke", value="Erase messages (confirmation required).", inline=False)
    await interaction.response.send_message(embed=embed)

if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN)
