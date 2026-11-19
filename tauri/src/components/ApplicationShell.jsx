// src/components/ApplicationShell.jsx - Core desktop shell with proactive notifications
import React, { useState, useEffect } from 'react';
import TitleBar from './TitleBar';
import NavigationDrawer from './NavigationDrawer';
import WorkspaceContainer from './WorkspaceContainer';
import ConfirmationModal from './ConfirmationModal';
import NotificationToast from './NotificationToast';
import BriefingModal from './BriefingModal';
import UpdateModal from './UpdateModal';
import {
  sendMessage,
  confirmAction,
  onEvent,
  pingAdapter,
  getChatHistory,
  acknowledgeEvent,
  getUnacknowledgedEvents,
  getLatestBriefing,
  getLatestReconciliations
} from '../services/tauriBridge';
import {
  subscribeToUpdater,
  startPeriodicChecks,
  stopPeriodicChecks,
  openUpdateModal,
  UpdateStatus
} from '../services/updaterService';
import '../styles/ApplicationShell.css';

export default function ApplicationShell() {
  const [activeView, setActiveView] = useState('home');
  const [isDrawerOpen, setIsDrawerOpen] = useState(false);
  const [messages, setMessages] = useState([]);
  const [isExecuting, setIsExecuting] = useState(false);
  const [confirmation, setConfirmation] = useState(null);
  const [runtimeStatus, setRuntimeStatus] = useState('online');
  const [runtimePid, setRuntimePid] = useState(null);
  const [notifications, setNotifications] = useState([]);
  const [briefingModal, setBriefingModal] = useState(null);
  const [updaterState, setUpdaterState] = useState(null);

  // Stage 4.5B Phase 4: Subscribe to updater service and initiate non-intrusive periodic checks
  useEffect(() => {
    const unsub = subscribeToUpdater((state) => {
      setUpdaterState(state);
      if (state.status === UpdateStatus.UPDATE_AVAILABLE) {
        addNotification({
          id: `update_${state.targetVersion}`,
          event_id: `update_${state.targetVersion}`,
          event_type: 'update.available',
          title: 'Software Update Available',
          message: `Orange AI v${state.targetVersion} is ready to download. Click to review changelog.`,
          payload: state,
          created_at: new Date().toISOString()
        });
      }
    });

    startPeriodicChecks();

    return () => {
      unsub();
      stopPeriodicChecks();
    };
  }, []);

  const addNotification = (item) => {
    setNotifications((prev) => {
      const key = item.event_id || item.id;
      if (prev.some((n) => (n.event_id || n.id) === key)) {
        return prev;
      }
      return [item, ...prev].slice(0, 5);
    });
  };

  const handleBriefingReady = (payload) => {
    const envelope = payload?.event_id ? payload : { payload, event_id: `evt_briefing_${payload?.id || Date.now()}`, event_type: 'briefing.ready' };
    const briefingData = envelope.payload || payload;
    const item = {
      id: envelope.event_id,
      event_id: envelope.event_id,
      event_type: 'briefing.ready',
      title: 'Morning Briefing Ready',
      message: briefingData.content ? `${briefingData.content.slice(0, 90)}...` : `Briefing for ${briefingData.local_date || 'today'} is available.`,
      payload: briefingData,
      created_at: envelope.created_at || new Date().toISOString()
    };
    addNotification(item);
  };

  const handleReminderDue = (payload) => {
    const envelope = payload?.event_id ? payload : { payload, event_id: `evt_reminder_${payload?.reminder_id || Date.now()}`, event_type: 'reminder.due' };
    const remData = envelope.payload || payload;
    const item = {
      id: envelope.event_id,
      event_id: envelope.event_id,
      event_type: 'reminder.due',
      title: 'Reminder Due',
      message: remData.message || 'A scheduled reminder is due.',
      payload: remData,
      created_at: envelope.created_at || new Date().toISOString()
    };
    addNotification(item);
  };

  const handleRecommendationCreated = (payload) => {
    const envelope = payload?.event_id ? payload : { payload, event_id: `evt_rec_${payload?.id || Date.now()}`, event_type: 'recommendation.created' };
    const recData = envelope.payload || payload;
    const item = {
      id: envelope.event_id,
      event_id: envelope.event_id,
      event_type: 'recommendation.created',
      title: 'New Advisory Available',
      message: recData.title || recData.description || 'Contextual recommendation evaluated.',
      payload: recData,
      created_at: envelope.created_at || new Date().toISOString()
    };
    addNotification(item);
  };

  const handleReconciliationReady = (payload) => {
    const envelope = payload?.event_id ? payload : { payload, event_id: `evt_recon_${payload?.id || Date.now()}`, event_type: 'reconciliation.ready' };
    const reconData = envelope.payload || payload;
    const item = {
      id: envelope.event_id,
      event_id: envelope.event_id,
      event_type: 'reconciliation.ready',
      title: 'Workspace Reconciliation Ready',
      message: `${reconData.workspace_name || 'Project'}: status ${(reconData.status || 'clean').toUpperCase()}`,
      payload: reconData,
      created_at: envelope.created_at || new Date().toISOString()
    };
    addNotification(item);
  };

  // Monitor Python runtime status on mount and register global shortcuts & event listeners
  useEffect(() => {
    const unlisteners = [];

    const handleGlobalKeyDown = (e) => {
      // Toggle theme with Ctrl+Shift+T or Alt+T
      if ((e.ctrlKey && e.shiftKey && e.key.toLowerCase() === 't') || (e.altKey && e.key.toLowerCase() === 't')) {
        e.preventDefault();
        window.__orangeAiTheme?.toggleTheme();
      }
      // Toggle drawer with Ctrl+Shift+D or Alt+M
      if ((e.ctrlKey && e.shiftKey && e.key.toLowerCase() === 'd') || (e.altKey && e.key.toLowerCase() === 'm')) {
        e.preventDefault();
        setIsDrawerOpen((prev) => !prev);
      }
    };
    window.addEventListener('keydown', handleGlobalKeyDown);

    async function initRuntime() {
      try {
        const ping = await pingAdapter();
        if (ping && ping.pid) {
          setRuntimePid(ping.pid);
          setRuntimeStatus(ping.status || 'online');
        }

        const hist = await getChatHistory(40);
        if (Array.isArray(hist) && hist.length > 0) {
          setMessages((prev) => {
            if (prev.length === 0) {
              return hist.map((h) => ({
                role: h.role,
                text: h.content,
                timestamp: h.created_at ? new Date(h.created_at).getTime() : Date.now()
              }));
            }
            return prev;
          });
        }
      } catch (err) {
        console.warn('Initial ping failed:', err);
      }

      // Replay unacknowledged proactive events from SQLite
      try {
        const unacked = await getUnacknowledgedEvents(25);
        if (Array.isArray(unacked)) {
          for (const evt of unacked) {
            if (evt.event_type === 'briefing.ready' || evt.event_type === 'briefing_ready') {
              handleBriefingReady(evt);
            } else if (evt.event_type === 'reminder.due' || evt.event_type === 'reminder_due') {
              handleReminderDue(evt);
            } else if (evt.event_type === 'recommendation.created' || evt.event_type === 'recommendation_created') {
              handleRecommendationCreated(evt);
            } else if (evt.event_type === 'reconciliation.ready' || evt.event_type === 'reconciliation_ready') {
              handleReconciliationReady(evt);
            }
          }
        }
      } catch (e) {
        console.warn('Failed to replay unacknowledged events:', e);
      }

      // Listen for runtime_ready
      const uReady = await onEvent('runtime_ready', (payload) => {
        setRuntimeStatus('online');
        if (payload?.pid) setRuntimePid(payload.pid);
      });
      unlisteners.push(uReady);

      // Listen for interactive confirmation events
      const uConfirm = await onEvent('assistant_confirmation_required', (payload) => {
        setConfirmation(payload);
      });
      unlisteners.push(uConfirm);

      // Listen for Stage 2D proactive events
      const uBriefing = await onEvent('briefing_ready', handleBriefingReady);
      unlisteners.push(uBriefing);

      const uReminder = await onEvent('reminder_due', handleReminderDue);
      unlisteners.push(uReminder);

      const uRecCreated = await onEvent('recommendation_created', handleRecommendationCreated);
      unlisteners.push(uRecCreated);

      const uRecon = await onEvent('reconciliation_ready', handleReconciliationReady);
      unlisteners.push(uRecon);

      // Listen for UI control events
      const uTheme = await onEvent('ui_set_theme', (payload) => {
        if (payload?.theme) window.__orangeAiTheme?.setTheme(payload.theme);
      });
      unlisteners.push(uTheme);

      const uDrawer = await onEvent('ui_toggle_drawer', (payload) => {
        setIsDrawerOpen((prev) => (payload?.open !== undefined ? payload.open : !prev));
      });
      unlisteners.push(uDrawer);

      const uView = await onEvent('ui_select_view', (payload) => {
        if (payload?.view) setActiveView(payload.view);
      });
      unlisteners.push(uView);
    }

    initRuntime();

    return () => {
      window.removeEventListener('keydown', handleGlobalKeyDown);
      unlisteners.forEach((u) => {
        try { if (typeof u === 'function') u(); } catch (_) {}
      });
    };
  }, []);

  const handleToggleDrawer = () => {
    setIsDrawerOpen((prev) => !prev);
  };

  const handleCloseDrawer = () => {
    setIsDrawerOpen(false);
  };

  const handleSelectView = (viewId) => {
    setActiveView(viewId);
  };

  const handleToastAction = (item) => {
    if (item.event_type === 'briefing.ready' || item.event_type === 'briefing_ready') {
      setBriefingModal(item.payload);
    } else if (item.event_type === 'reminder.due' || item.event_type === 'reminder_due') {
      if (item.event_id) {
        acknowledgeEvent(item.event_id, item.event_type).catch(console.error);
      }
      setNotifications((prev) => prev.filter((n) => (n.event_id || n.id) !== (item.event_id || item.id)));
    } else if (item.event_type === 'recommendation.created' || item.event_type === 'recommendation_created') {
      setActiveView('chat');
      setMessages((prev) => {
        if (!prev.some((m) => m.recommendation?.id === item.payload?.id)) {
          return [
            ...prev,
            {
              role: 'assistant',
              text: `💡 **Orange AI Advisory**: ${item.payload?.title || 'Contextual Recommendation'}`,
              recommendation: item.payload,
              timestamp: Date.now()
            }
          ];
        }
        return prev;
      });
      setNotifications((prev) => prev.filter((n) => (n.event_id || n.id) !== (item.event_id || item.id)));
    } else if (item.event_type === 'reconciliation.ready' || item.event_type === 'reconciliation_ready') {
      setActiveView('activity');
      setNotifications((prev) => prev.filter((n) => (n.event_id || n.id) !== (item.event_id || item.id)));
    } else if (item.event_type === 'update.available') {
      openUpdateModal();
      setNotifications((prev) => prev.filter((n) => (n.event_id || n.id) !== (item.event_id || item.id)));
    }
  };

  const handleToastDismiss = (item) => {
    if (item.event_id) {
      acknowledgeEvent(item.event_id, item.event_type).catch(console.error);
    }
    setNotifications((prev) => prev.filter((n) => (n.event_id || n.id) !== (item.event_id || item.id)));
  };

  const handleAcknowledgeBriefing = (briefing) => {
    const evtId = `evt_briefing_${briefing.local_date}`;
    acknowledgeEvent(evtId, 'briefing.ready').catch(console.error);
    setNotifications((prev) => prev.filter((n) => n.event_type?.includes('briefing')));
  };

  // Expose test & automation bridge on window
  useEffect(() => {
    window.__orangeAi = {
      toggleDrawer: handleToggleDrawer,
      openDrawer: () => setIsDrawerOpen(true),
      closeDrawer: handleCloseDrawer,
      selectView: handleSelectView,
      sendMessage: handleSendMessage,
      setMessages: (newMessages) => {
        if (typeof newMessages === 'function') {
          setMessages(newMessages);
        } else if (Array.isArray(newMessages)) {
          setMessages(newMessages);
        }
      },
      getMessages: () => messages,
      getActiveView: () => activeView,
      isDrawerOpen: () => isDrawerOpen,
      getRuntimeStatus: () => runtimeStatus,
      triggerConfirmation: (payload) => setConfirmation(payload),
      resolveConfirmation: (decision) => {
        if (confirmation) handleConfirmDecision(confirmation.confirmation_id, decision);
      },
      // Stage 2D test & inspection helpers
      getNotifications: () => notifications,
      dismissNotification: (id) => {
        const item = notifications.find((n) => (n.event_id || n.id) === id);
        if (item) handleToastDismiss(item);
      },
      triggerNotification: (type, payload) => {
        if (type.includes('briefing')) handleBriefingReady(payload);
        else if (type.includes('reminder')) handleReminderDue(payload);
        else if (type.includes('recommendation')) handleRecommendationCreated(payload);
        else if (type.includes('reconciliation')) handleReconciliationReady(payload);
      },
      openBriefing: (briefing) => setBriefingModal(briefing),
      closeBriefing: () => setBriefingModal(null),
      getActiveBriefing: () => briefingModal,
      acknowledgeBriefing: (briefing) => handleAcknowledgeBriefing(briefing)
    };
    return () => {
      delete window.__orangeAi;
    };
  }, [activeView, isDrawerOpen, notifications, briefingModal]);

  const handleSendMessage = async (text) => {
    if (!text.trim() || isExecuting) return;

    // Add user message
    const userMsg = { role: 'user', text, timestamp: Date.now() };
    setMessages((prev) => [...prev, userMsg]);
    setIsExecuting(true);

    // Switch to chat view so user can follow the conversation
    setActiveView('chat');

    try {
      const result = await sendMessage(text);
      let replyText = result?.reply || result?.response || 'I am ready to assist.';
      
      // If structured recommendation is returned, clean up embedded text block
      const recommendation = result?.recommendation || null;
      if (recommendation && replyText.includes('Orange AI Advisory')) {
        const parts = replyText.split(/\n+💡\s+\*\*Orange AI Advisory\*\*/);
        if (parts[0].trim()) {
          replyText = parts[0].trim();
        }
      }

      const assistantMsg = {
        role: 'assistant',
        text: replyText,
        model: result?.model || null,
        recommendation,
        tool_result: result?.tool_result || null,
        timestamp: Date.now()
      };
      setMessages((prev) => [...prev, assistantMsg]);
    } catch (err) {
      console.error('Failed to send message:', err);
      const errorMsg = {
        role: 'assistant',
        text: `Error executing request: ${err.message || String(err)}`,
        model: 'error',
        timestamp: Date.now()
      };
      setMessages((prev) => [...prev, errorMsg]);
    } finally {
      setIsExecuting(false);
    }
  };

  const handleConfirmDecision = async (confirmationId, decision) => {
    try {
      await confirmAction(confirmationId, decision);
    } catch (err) {
      console.error('Failed to confirm action:', err);
    } finally {
      setConfirmation(null);
    }
  };

  return (
    <div className="application-shell">
      {/* Frameless Windows TitleBar */}
      <TitleBar />

      {/* Main Workspace Area */}
      <div className="workspace-viewport">
        <WorkspaceContainer
          activeView={activeView}
          onToggleDrawer={handleToggleDrawer}
          isDrawerOpen={isDrawerOpen}
          messages={messages}
          onSendMessage={handleSendMessage}
          isExecuting={isExecuting}
          userName="Sayan"
        />

        {/* Overlay Navigation Drawer */}
        <NavigationDrawer
          isOpen={isDrawerOpen}
          onClose={handleCloseDrawer}
          activeView={activeView}
          onSelectView={handleSelectView}
          runtimeStatus={runtimeStatus}
          runtimePid={runtimePid}
        />
      </div>

      {/* Floating Proactive Notification Toasts */}
      <NotificationToast
        notifications={notifications}
        onDismiss={handleToastDismiss}
        onAction={handleToastAction}
      />

      {/* Morning Briefing Presentation Modal */}
      <BriefingModal
        briefing={briefingModal}
        isOpen={Boolean(briefingModal)}
        onClose={() => setBriefingModal(null)}
        onAcknowledge={handleAcknowledgeBriefing}
      />

      {/* Two-Phase Confirmation Modal */}
      <ConfirmationModal
        confirmation={confirmation}
        onConfirm={handleConfirmDecision}
      />

      {/* Option 1 Updater Modal */}
      <UpdateModal updaterState={updaterState} />
    </div>
  );
}
