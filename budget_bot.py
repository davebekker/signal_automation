import asyncio
import json
import logging
import os
from datetime import datetime, timedelta

# --- Configuration ---
STATE_FILE = "budget_state.json"

class BudgetBot:
    def __init__(self):
        self.state = self.load_state()

    def load_state(self):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {
                "balance": 0.0,
                "weekly_amount": 1.0, 
                "last_weekly_update": datetime.now().strftime("%Y-%m-%d"),
                "history": []
            }

    def save_state(self):
        with open(STATE_FILE, "w") as f:
            json.dump(self.state, f, indent=4)

    async def weekly_task(self, alert_callback):
        while True:
            try:
                now = datetime.now()
                # Convert stored string to datetime object
                last_date = datetime.strptime(self.state["last_weekly_update"], "%Y-%m-%d")
                
                # 1. Calculate the next scheduled update (7 days after the last one)
                next_update = last_date + timedelta(days=7)
                
                # 2. If it's time (or past time) for the update
                if now >= next_update:
                    # Calculate how many weeks have passed in case the bot was offline
                    days_passed = (now - last_date).days
                    weeks = days_passed // 7
                    
                    if weeks > 0:
                        total = weeks * self.state["weekly_amount"]
                        self.add_transaction(total, f"Auto-allowance ({weeks} wks)")
                        
                        # Update state to the most recent completed week
                        new_last_update = last_date + timedelta(weeks=weeks)
                        self.state["last_weekly_update"] = new_last_update.strftime("%Y-%m-%d")
                        self.save_state()
                        
                        logging.info(f"Automatically added £{total} to budget")
                        await alert_callback(f"💰 **Weekly Allowance**: Added £{total} to your budget.")
                        
                        # Recalculate next_update for the sleep duration
                        next_update = new_last_update + timedelta(days=7)

                # 3. Sleep until the next scheduled update
                wait_seconds = (next_update - datetime.now()).total_seconds()
                
                if wait_seconds > 0:
                    logging.info(f"BudgetBot: Sleeping for {wait_seconds/3600:.1f} hours until next allowance.")
                    await asyncio.sleep(wait_seconds)
                else:
                    # Safety fallback if calculation results in a negative number
                    await asyncio.sleep(3600)

            except Exception as e:
                logging.error(f"Error in weekly_task: {e}")
                await asyncio.sleep(3600) # Wait an hour before retrying on error

    def add_transaction(self, amount, comment):
        self.state["balance"] += amount
        self.state["history"].append({
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "amount": amount,
            "comment": comment if comment else "Manual Entry"
        })
        self.state["history"] = self.state["history"][-10:]
        self.save_state()
        return self.state["balance"]

    async def handle_command(self, text):
        parts = text.split()
        if not parts: return None
        cmd = parts[0].lower()
        
        try:
            if cmd in ["/usage", "/help"]:
                return (
                    "📖 *Budget Bot Usage*\n"
                    "• /balance - Show current balance\n"
                    "• /add [amount] [reason] - Add funds\n"
                    "• /sub [amount] [reason] - Withdraw\n"
                    "• /history - Show last 10 transactions\n"
                    "• /set [amount] - Change weekly allowance\n"
                    "• /usage - Show this menu"
                )
            elif cmd == "/balance":
                return f"💰 Balance: £{self.state['balance']:.2f}"
            
            elif cmd == "/history":
                if not self.state["history"]:
                    return "📜 No transactions yet."
                h_lines = [f"• {h['date']}: £{h['amount']:.2f} ({h['comment']})" for h in self.state["history"]]
                return "📜 Recent History:\n" + "\n".join(h_lines)

            elif cmd in ["/add", "/sub", "/withdraw"] and len(parts) > 1:
                try:
                    amt = float(parts[1])
                    comment = " ".join(parts[2:]) if len(parts) > 2 else ""
                    
                    if cmd in ["/sub", "/withdraw"]:
                        amt = -amt
                        action = "Subtracted"
                    else:
                        action = "Added"
                        
                    self.add_transaction(amt, comment)
                    return f"✅ {action} £{abs(amt):.2f}. New Balance: £{self.state['balance']:.2f}"
                except ValueError:
                    return "⚠️ Invalid amount. Use: /add 5.00 chocolate"

            elif cmd == "/set" and len(parts) > 1:
                self.state["weekly_amount"] = float(parts[1])
                self.save_state()
                return f"⚙️ Weekly amount set to £{self.state['weekly_amount']:.2f}"
                
        except Exception as e:
            return f"⚠️ Error: {str(e)}"
        return None