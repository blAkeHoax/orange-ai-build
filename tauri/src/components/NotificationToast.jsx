// src/components/NotificationToast.jsx - Floating proactive notification toasts for Orange AI Desktop 2.0
import React from 'react';
import { Sun, Bell, Sparkles, GitBranch, X, ExternalLink, Check } from 'lucide-react';
import '../styles/NotificationToast.css';

export default function NotificationToast({
  notifications = [],
  onDismiss,
  onAction
}) {
  if (!notifications || notifications.length === 0) return null;

  const getIcon = (type) => {
    switch (type) {
      case 'briefing.ready':
      case 'briefing_ready':
        return <Sun size={16} className="toast-icon briefing-icon" />;
      case 'reminder.due':
      case 'reminder_due':
        return <Bell size={16} className="toast-icon reminder-icon" />;
      case 'recommendation.created':
      case 'recommendation_created':
      case 'recommendation.updated':
      case 'recommendation_updated':
        return <Sparkles size={16} className="toast-icon rec-icon" />;
      case 'reconciliation.ready':
      case 'reconciliation_ready':
        return <GitBranch size={16} className="toast-icon recon-icon" />;
      default:
        return <Bell size={16} className="toast-icon default-icon" />;
    }
  };

  const getActionLabel = (type) => {
    switch (type) {
      case 'briefing.ready':
      case 'briefing_ready':
        return 'View Briefing';
      case 'reminder.due':
      case 'reminder_due':
        return 'Acknowledge';
      case 'recommendation.created':
      case 'recommendation_created':
        return 'View Advisory';
      case 'reconciliation.ready':
      case 'reconciliation_ready':
        return 'View Activity';
      default:
        return 'Details';
    }
  };

  return (
    <aside className="toast-container" aria-label="Notifications" role="region">
      {notifications.map((item) => {
        const actionLabel = getActionLabel(item.event_type);
        return (
          <div
            key={item.event_id || item.id}
            className={`toast-card toast-${(item.event_type || '').replace(/[._]/g, '-')}`}
            role="status"
            aria-live="polite"
          >
            <div className="toast-header">
              <div className="toast-title-row">
                {getIcon(item.event_type)}
                <span className="toast-title">{item.title}</span>
              </div>
              <button
                className="toast-close-btn"
                onClick={() => onDismiss && onDismiss(item)}
                title="Dismiss visual notification"
                type="button"
                aria-label="Dismiss notification"
              >
                <X size={13} />
              </button>
            </div>

            <p className="toast-message">{item.message}</p>

            <div className="toast-footer">
              <button
                className="toast-action-btn"
                onClick={() => onAction && onAction(item)}
                type="button"
              >
                <span>{actionLabel}</span>
                <ExternalLink size={12} />
              </button>
            </div>
          </div>
        );
      })}
    </aside>
  );
}
