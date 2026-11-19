// src/components/RecommendationCard.jsx - Presentation card with Stage 2D lifecycle controls
import React, { useState } from 'react';
import {
  Sparkles,
  ChevronDown,
  ChevronUp,
  AlertTriangle,
  Info,
  ShieldAlert,
  Play,
  Clock,
  EyeOff,
  CheckCircle,
  XCircle,
  HelpCircle,
  Loader2
} from 'lucide-react';
import {
  dismissRecommendation,
  snoozeRecommendation,
  executeRecommendation
} from '../services/tauriBridge';

export default function RecommendationCard({
  recommendation,
  onDismiss: propOnDismiss,
  onSnooze: propOnSnooze,
  onExecute: propOnExecute
}) {
  const [evidenceExpanded, setEvidenceExpanded] = useState(false);
  const [currentStatus, setCurrentStatus] = useState(recommendation?.status || 'ACTIVE');
  const [suppressedUntil, setSuppressedUntil] = useState(recommendation?.suppressed_until || null);
  const [executing, setExecuting] = useState(false);
  const [confirmationReq, setConfirmationReq] = useState(null);
  const [outcome, setOutcome] = useState(null);
  const [opLoading, setOpLoading] = useState(false);

  if (!recommendation) return null;

  const recId = recommendation.id;
  const title = recommendation.title || recommendation.suggested_action || 'Contextual Recommendation';
  const reason = recommendation.reason || recommendation.explanation || '';
  const priority = (recommendation.priority || 'MEDIUM').toUpperCase();
  const evidence = recommendation.evidence || null;

  const hasEvidence =
    evidence &&
    (typeof evidence === 'object'
      ? Object.keys(evidence).length > 0
      : String(evidence).trim().length > 0);

  const getPriorityBadge = (p) => {
    switch (p) {
      case 'HIGH':
        return (
          <span className="rec-priority-badge is-high">
            <ShieldAlert size={11} />
            <span>HIGH PRIORITY</span>
          </span>
        );
      case 'LOW':
        return (
          <span className="rec-priority-badge is-low">
            <Info size={11} />
            <span>LOW PRIORITY</span>
          </span>
        );
      case 'MEDIUM':
      default:
        return (
          <span className="rec-priority-badge is-medium">
            <AlertTriangle size={11} />
            <span>MEDIUM PRIORITY</span>
          </span>
        );
    }
  };

  const handleDismiss = async () => {
    if (opLoading || executing) return;
    setOpLoading(true);
    try {
      if (propOnDismiss) {
        await propOnDismiss(recId);
      } else {
        const res = await dismissRecommendation(recId);
        if (res) {
          setCurrentStatus(res.status || 'DISMISSED');
          setSuppressedUntil(res.suppressed_until || null);
        }
      }
      setCurrentStatus('DISMISSED');
    } catch (err) {
      console.error('Failed to dismiss recommendation:', err);
    } finally {
      setOpLoading(false);
    }
  };

  const handleSnooze = async () => {
    if (opLoading || executing) return;
    setOpLoading(true);
    try {
      if (propOnSnooze) {
        await propOnSnooze(recId);
      } else {
        const res = await snoozeRecommendation(recId, '24h');
        if (res) {
          setCurrentStatus(res.status || 'DEFERRED');
          setSuppressedUntil(res.suppressed_until || null);
        }
      }
      setCurrentStatus('DEFERRED');
    } catch (err) {
      console.error('Failed to snooze recommendation:', err);
    } finally {
      setOpLoading(false);
    }
  };

  const handleExecute = async () => {
    if (executing || opLoading) return;
    setExecuting(true);
    setOutcome(null);
    try {
      let res;
      if (propOnExecute) {
        res = await propOnExecute(recId);
      } else {
        res = await executeRecommendation(recId);
      }

      if (res?.status === 'REQUIRES_CONFIRMATION') {
        setConfirmationReq(res);
      } else {
        setOutcome(res);
        if (res?.status === 'SUCCESS') {
          setCurrentStatus('HANDLED');
        }
      }
    } catch (err) {
      console.error('Execution error:', err);
      setOutcome({ status: 'ERROR', error: err.message || String(err) });
    } finally {
      setExecuting(false);
    }
  };

  const handleConfirmDecision = async (confirmed) => {
    if (!confirmationReq) return;
    setExecuting(true);
    try {
      const res = await executeRecommendation(recId, confirmed, confirmationReq.confirmation_token);
      setConfirmationReq(null);
      setOutcome(res);
      if (res?.status === 'SUCCESS') {
        setCurrentStatus('HANDLED');
      }
    } catch (err) {
      console.error('Confirmation execution error:', err);
      setOutcome({ status: 'ERROR', error: err.message || String(err) });
    } finally {
      setExecuting(false);
    }
  };

  return (
    <div className={`recommendation-card priority-${priority.toLowerCase()}`}>
      {/* Header Row */}
      <div className="rec-card-header">
        <div className="rec-header-left">
          <Sparkles size={14} className="rec-sparkle-icon" />
          <span className="rec-brand-label">Orange AI Advisory</span>
        </div>
        {getPriorityBadge(priority)}
      </div>

      {/* Suggested Action / Title */}
      <h4 className="rec-action-title">{title}</h4>

      {/* Reasoning Explanation */}
      {reason && <p className="rec-reason-text">{reason}</p>}

      {/* Expandable Evidence Section */}
      {hasEvidence && (
        <div className="rec-evidence-section">
          <button
            className="rec-evidence-toggle"
            onClick={() => setEvidenceExpanded(!evidenceExpanded)}
            type="button"
            title={evidenceExpanded ? 'Hide evidence' : 'Show evidence'}
          >
            <span>Context &amp; Evidence</span>
            {evidenceExpanded ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
          </button>

          {evidenceExpanded && (
            <div className="rec-evidence-body">
              {typeof evidence === 'object' && !Array.isArray(evidence) ? (
                <ul className="rec-evidence-list">
                  {Object.entries(evidence).map(([key, val], idx) => (
                    <li key={idx} className="rec-evidence-item">
                      <strong className="rec-evidence-key">{key.replace(/_/g, ' ')}:</strong>{' '}
                      <span className="rec-evidence-val">
                        {typeof val === 'object' ? JSON.stringify(val) : String(val)}
                      </span>
                    </li>
                  ))}
                </ul>
              ) : (
                <pre className="rec-evidence-pre">
                  <code>
                    {typeof evidence === 'string'
                      ? evidence
                      : JSON.stringify(evidence, null, 2)}
                  </code>
                </pre>
              )}
            </div>
          )}
        </div>
      )}

      {/* Confirmation Handshake Prompt */}
      {confirmationReq && (
        <div className="rec-confirmation-box" role="alert">
          <div className="rec-confirmation-header">
            <AlertTriangle size={14} className="rec-warning-icon" />
            <span className="rec-confirmation-title">Permission Confirmation Required</span>
          </div>
          <p className="rec-confirmation-prompt">
            {confirmationReq.prompt || `Tool '${confirmationReq.tool_name}' requires explicit confirmation. Proceed?`}
          </p>
          <div className="rec-confirmation-actions">
            <button
              className="action-btn-secondary rec-conf-btn"
              onClick={() => handleConfirmDecision(false)}
              disabled={executing}
              type="button"
            >
              Cancel
            </button>
            <button
              className="action-btn-primary rec-conf-btn"
              onClick={() => handleConfirmDecision(true)}
              disabled={executing}
              type="button"
            >
              {executing ? <Loader2 size={12} className="spinning" /> : <CheckCircle size={12} />}
              <span>Confirm &amp; Run</span>
            </button>
          </div>
        </div>
      )}

      {/* Outcome Status Banner */}
      {outcome && (
        <div className={`rec-outcome-banner is-${(outcome.status || 'unknown').toLowerCase()}`}>
          <div className="rec-outcome-header">
            {outcome.status === 'SUCCESS' && <CheckCircle size={13} />}
            {outcome.status === 'DENIED' && <XCircle size={13} />}
            {outcome.status === 'UNSUPPORTED' && <HelpCircle size={13} />}
            {outcome.status === 'ERROR' && <AlertTriangle size={13} />}
            <span className="rec-outcome-status">{outcome.status}</span>
          </div>
          <p className="rec-outcome-detail">
            {outcome.status === 'SUCCESS' && (
              outcome.result ? (typeof outcome.result === 'object' ? JSON.stringify(outcome.result) : String(outcome.result)) : 'Action completed successfully.'
            )}
            {outcome.status === 'DENIED' && (outcome.reason || 'Execution was denied by safety policies.')}
            {outcome.status === 'UNSUPPORTED' && (outcome.error || 'Action type is advisory only and has no automated execution pathway.')}
            {outcome.status === 'ERROR' && (outcome.error || 'Execution encountered an unexpected error.')}
          </p>
        </div>
      )}

      {/* Stage 2D Lifecycle Action Buttons */}
      <div className="rec-card-lifecycle-row rec-lifecycle-actions">
        {(currentStatus === 'ACTIVE' || currentStatus === 'PENDING' || !['DISMISSED', 'DEFERRED', 'HANDLED'].includes(currentStatus)) && !confirmationReq && (
          <>
            <button
              className="rec-lifecycle-btn rec-dismiss-btn"
              onClick={handleDismiss}
              disabled={opLoading || executing}
              title="Suppress this advisory for 7 days"
              type="button"
            >
              <EyeOff size={12} />
              <span>Dismiss (7d)</span>
            </button>

            <button
              className="rec-lifecycle-btn rec-snooze-btn"
              onClick={handleSnooze}
              disabled={opLoading || executing}
              title="Snooze this advisory for 24 hours"
              type="button"
            >
              <Clock size={12} />
              <span>Snooze (24h)</span>
            </button>

            <button
              className="rec-lifecycle-btn rec-execute-btn"
              onClick={handleExecute}
              disabled={executing || opLoading}
              title="Execute approved action via ToolRegistry"
              type="button"
            >
              {executing ? <Loader2 size={12} className="spinning" /> : <Play size={12} />}
              <span>{executing ? 'Executing...' : 'Execute'}</span>
            </button>
          </>
        )}

        {currentStatus === 'DISMISSED' && (
          <span className="rec-state-tag is-dismissed">
            <EyeOff size={11} />
            <span>Dismissed (7-day suppression)</span>
          </span>
        )}

        {currentStatus === 'DEFERRED' && (
          <span className="rec-state-tag is-deferred">
            <Clock size={11} />
            <span>Snoozed (24-hour suppression)</span>
          </span>
        )}

        {currentStatus === 'HANDLED' && (
          <span className="rec-state-tag is-handled">
            <CheckCircle size={11} />
            <span>Action Executed &amp; Handled</span>
          </span>
        )}
      </div>
    </div>
  );
}
