"""
Notifications Module
Send alerts when machines finish or become available
"""

import time
import smtplib
import threading
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Dict, Set, Optional, Callable
from dataclasses import dataclass
import requests
import logging

from state_machine import MachineState, MachineStatus

logger = logging.getLogger(__name__)


@dataclass
class Subscription:
    """User subscription for notifications"""
    id: str
    email: Optional[str] = None
    webhook_url: Optional[str] = None
    watch_aggregator: Optional[int] = None  # None = all
    watch_machine: Optional[int] = None     # None = all in aggregator
    notify_on_done: bool = True
    notify_on_free: bool = False
    notify_any_free: bool = False           # Notify when ANY machine becomes free
    created_at: float = 0.0


class NotificationManager:
    """Manages subscriptions and sends notifications"""
    
    def __init__(self, config: dict):
        self.config = config.get("notifications", {})
        self.enabled = self.config.get("enabled", False)
        self.subscriptions: Dict[str, Subscription] = {}
        self.lock = threading.Lock()
        
        # Track which machines we've notified about to avoid duplicates
        self.notified_done: Set[tuple] = set()
        
    def add_subscription(self, subscription: Subscription) -> str:
        """Add a new subscription"""
        subscription.created_at = time.time()
        
        with self.lock:
            self.subscriptions[subscription.id] = subscription
            
        logger.info(f"Added subscription: {subscription.id}")
        return subscription.id
        
    def remove_subscription(self, subscription_id: str) -> bool:
        """Remove a subscription"""
        with self.lock:
            if subscription_id in self.subscriptions:
                del self.subscriptions[subscription_id]
                logger.info(f"Removed subscription: {subscription_id}")
                return True
        return False
        
    def on_state_change(self, machine: MachineStatus, old_state: MachineState, 
                       new_state: MachineState):
        """Handle a machine state change"""
        if not self.enabled:
            return
            
        key = (machine.aggregator_id, machine.machine_id)
        
        # Notify when machine becomes DONE
        if new_state == MachineState.DONE:
            if key not in self.notified_done:
                self.notified_done.add(key)
                self._send_done_notifications(machine)
                
        # Notify when machine becomes FREE (if subscribed)
        elif new_state == MachineState.FREE:
            self._send_free_notifications(machine)
            
        # Clear notification flag when machine starts running again
        elif new_state == MachineState.RUNNING:
            self.notified_done.discard(key)
            
    def _send_done_notifications(self, machine: MachineStatus):
        """Send notifications for machine done"""
        with self.lock:
            for sub in self.subscriptions.values():
                if not sub.notify_on_done:
                    continue
                    
                # Check if subscription matches this machine
                if sub.watch_aggregator and sub.watch_aggregator != machine.aggregator_id:
                    continue
                if sub.watch_machine and sub.watch_machine != machine.machine_id:
                    continue
                    
                self._send_notification(
                    sub,
                    f"🟢 {machine.name} is likely done!",
                    f"The washing machine '{machine.name}' appears to have finished.\n"
                    f"Location: Aggregator {machine.aggregator_id}\n"
                    f"Please collect your laundry."
                )
                
    def _send_free_notifications(self, machine: MachineStatus):
        """Send notifications for machine free"""
        with self.lock:
            for sub in self.subscriptions.values():
                if not sub.notify_on_free and not sub.notify_any_free:
                    continue
                    
                # Check if subscription matches
                if sub.notify_any_free:
                    # Any free notification - check aggregator filter
                    if sub.watch_aggregator and sub.watch_aggregator != machine.aggregator_id:
                        continue
                elif sub.notify_on_free:
                    # Specific machine notification
                    if sub.watch_aggregator and sub.watch_aggregator != machine.aggregator_id:
                        continue
                    if sub.watch_machine and sub.watch_machine != machine.machine_id:
                        continue
                        
                self._send_notification(
                    sub,
                    f"🔵 {machine.name} is now free!",
                    f"The washing machine '{machine.name}' is now available.\n"
                    f"Location: Aggregator {machine.aggregator_id}"
                )
                
    def _send_notification(self, subscription: Subscription, subject: str, body: str):
        """Send a notification via configured channel"""
        # Send via webhook
        if subscription.webhook_url:
            self._send_webhook(subscription.webhook_url, subject, body)
            
        # Send via email
        if subscription.email:
            self._send_email(subscription.email, subject, body)
            
    def _send_webhook(self, url: str, subject: str, body: str):
        """Send notification via webhook"""
        try:
            payload = {
                "title": subject,
                "message": body,
                "timestamp": time.time()
            }
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            logger.info(f"Webhook notification sent to {url}")
        except Exception as e:
            logger.error(f"Failed to send webhook: {e}")
            
    def _send_email(self, to_email: str, subject: str, body: str):
        """Send notification via email"""
        if not self.config.get("email_smtp_server"):
            logger.warning("Email not configured")
            return
            
        try:
            msg = MIMEMultipart()
            msg['From'] = self.config.get("email_from", "noreply@localhost")
            msg['To'] = to_email
            msg['Subject'] = subject
            msg.attach(MIMEText(body, 'plain'))
            
            server = smtplib.SMTP(
                self.config["email_smtp_server"],
                self.config.get("email_smtp_port", 587)
            )
            server.starttls()
            
            if self.config.get("email_username"):
                server.login(
                    self.config["email_username"],
                    self.config["email_password"]
                )
                
            server.send_message(msg)
            server.quit()
            
            logger.info(f"Email notification sent to {to_email}")
        except Exception as e:
            logger.error(f"Failed to send email: {e}")
