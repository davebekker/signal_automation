# 🤖 Signal Home Assistant Bot Suite

A multi-purpose, event-driven home assistant bot for Signal. This suite provides real-time train tracking, managing your kids pocket money, and council bin collection reminders using "sleep-to-event" architecture.

## 🌟 Key Features

### 🚆 TrainBot (Official National Rail Data)
- **Official Source**: Powered by Thales OpenLDBWS (Darwin) SOAP API.
- **Platform Tracking**: Live platform numbers and "TBC" status updates.
- **Sticky Context**: Remembers the last station queried so you can `/watch` a train without re-typing the station code.
- **Advanced Filtering**: Supports origin-to-destination queries (e.g., `/trains wat wim`).
- **Station Shortcuts**: Persistent `stations.json` storage for shortcuts like `home`, `work`, or `gym`.

### 🚛 BinBot (Council Scraper)
- **Persistent Caching**: Scrapes the council website and saves the full month's schedule to `bins.json`.
- **Dual Reminders**: Automatically sends a "Night Before" alert (18:00) and a "Morning Of" alert (07:00).
- **Efficient Scheduling**: Calculates the exact seconds until the next collection milestone and sleeps

### 💰 BudgetBot (Auto-Allowance)
- **Weekly Tasks**: Automatically adds a weekly allowance to your state.
- **Catch-up Logic**: If the bot is offline, it calculates how many weeks were missed and adds the cumulative total upon startup.
- **Persistence**: Saves all transactions and balance state to `budget_state.json`.

---

## 🚀 Commands

| Command | Description |
| :--- | :--- |
| `/trains [from] [to]` | Show departures. Supports shortcuts or CRS codes. |
| `/watch [time]` | Monitor a specific train for status/platform changes. |
| `/add [name] [CRS]` | Save a station shortcut (e.g., `/add work WAT`). |
| `/list` | Show all saved station shortcuts. |
| `/unwatch` | Clear active train subscriptions. |
| `/bins` | Show the next 4 weeks of bin collections. |
| `/budget` | Show current balance and last 5 transactions. |
| `[amount] [desc]` | Add a manual transaction (e.g., `5.50 lunch`). |

---

## 🛠 Technical Architecture

The project uses a **Master-Worker Callback Pattern** to handle asynchronous alerts across different domains.



- **MasterBot**: Manages the Signal-CLI REST API and routes incoming messages to the correct sub-bot.
- **Sub-Bots**: Class-based modules (`TrainBot`, `BinBot`, `BudgetBot`) that handle logic and state independently.
- **Alert System**: Sub-bots use an `alert_callback` to "push" notifications to the MasterBot whenever a background milestone is reached.

---

## ⚙️ Configuration (.env)

```env
#Main
SIGNAL_NUMBER="+1234567890"

#Budget bot
BUDGET_RECIPIENT="group.Signalgroupcode"
BUDGET_INTERNAL_ID="internal code"

#Train bot
LDB_TOKEN="<your rail ldb token>"
DEFAULT_CRS="the CRS of the station closest to you"
TRAIN_RECIPIENT="group.Signalgroupcode="
TRAIN_INTERNAL_ID="internal code"

#Bin bot
BIN_RECIPIENT="group.signalgroupcode"
BIN_INTERNAL_ID="internal code"
WASTE_URL="bespoke i'm afraid - you'll have to parse it yourself"
```