// src/components/BriefingModal.jsx - Morning Briefing Presentation Dialog
import React from 'react';
import { Sun, X, Check, ShieldCheck, AlertCircle, Calendar, Clock, Sparkles } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import '../styles/BriefingModal.css';

export default function BriefingModal({
  briefing,
  isOpen,
  onClose,
  onAcknowledge
}) {
  if (!isOpen || !briefing) return null;

  const isModel = briefing.status === 'success' && briefing.model_name;
  const isFallback = briefing.status === 'fallback';
  const isFailed = briefing.status === 'failed';

  const priorities = Array.isArray(briefing.priorities) ? briefing.priorities : [];
  const sources = briefing.sources && typeof briefing.sources === 'object' ? briefing.sources : {};

  return (
    <div className="briefing-modal-backdrop" onClick={onClose} role="dialog" aria-modal="true" aria-labelledby="briefing-dialog-title">
      <div className="briefing-modal-container" onClick={(e) => e.stopPropagation()}>
        {/* Modal Header */}
        <div className="briefing-modal-header">
          <div className="briefing-header-title-row">
            <Sun size={20} className="briefing-sun-icon" />
            <div>
              <h3 id="briefing-dialog-title" className="briefing-modal-title">
                Morning Briefing
              </h3>
              <div className="briefing-meta-row">
                <span className="briefing-meta-item">
                  <Calendar size={12} />
                  <span>{briefing.local_date}</span>
                </span>
                {briefing.created_at && (
                  <span className="briefing-meta-item">
                    <Clock size={12} />
                    <span>{new Date(briefing.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>
                  </span>
                )}
              </div>
            </div>
          </div>

          <div className="briefing-header-actions">
            {/* Model vs Deterministic Fallback status badge */}
            {isModel ? (
              <span className="briefing-status-badge is-model">
                <Sparkles size={11} />
                <span>Synthesized ({briefing.model_name})</span>
              </span>
            ) : isFallback ? (
              <span className="briefing-status-badge is-fallback">
                <ShieldCheck size={11} />
                <span>Deterministic Fallback</span>
              </span>
            ) : isFailed ? (
              <span className="briefing-status-badge is-failed">
                <AlertCircle size={11} />
                <span>Synthesis Failed</span>
              </span>
            ) : null}

            <button
              className="briefing-modal-close"
              onClick={onClose}
              type="button"
              aria-label="Close dialog"
            >
              <X size={16} />
            </button>
          </div>
        </div>

        {/* Modal Body */}
        <div className="briefing-modal-body">
          {/* Priorities Section if present */}
          {priorities.length > 0 && (
            <div className="briefing-priorities-card">
              <h4 className="briefing-section-title">Key Priorities for Today</h4>
              <ul className="briefing-priorities-list">
                {priorities.map((item, idx) => (
                  <li key={idx} className="briefing-priority-item">
                    <span className="briefing-priority-bullet" />
                    <span>{typeof item === 'object' ? item.text || JSON.stringify(item) : String(item)}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* Markdown Content */}
          <div className="briefing-content-container">
            {briefing.content ? (
              <ReactMarkdown remarkPlugins={[remarkGfm]}>
                {briefing.content}
              </ReactMarkdown>
            ) : (
              <p className="briefing-empty-content">
                {briefing.error_message ? `Synthesis notice: ${briefing.error_message}` : 'No briefing summary generated for this date.'}
              </p>
            )}
          </div>

          {/* Contextual Sources / Grounding */}
          {Object.keys(sources).length > 0 && (
            <div className="briefing-sources-section">
              <h5 className="briefing-sources-title">Contextual Sources &amp; Operational Baseline</h5>
              <div className="briefing-sources-grid">
                {Object.entries(sources).map(([k, v], idx) => (
                  <div key={idx} className="briefing-source-tag">
                    <strong>{k.replace(/_/g, ' ')}:</strong>{' '}
                    <span>{typeof v === 'object' ? JSON.stringify(v) : String(v)}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Modal Footer */}
        <div className="briefing-modal-footer">
          <button
            className="action-btn-secondary"
            onClick={onClose}
            type="button"
          >
            Dismiss
          </button>
          <button
            className="action-btn-primary"
            onClick={() => {
              if (onAcknowledge) onAcknowledge(briefing);
              onClose();
            }}
            type="button"
          >
            <Check size={14} />
            <span>Acknowledge Briefing</span>
          </button>
        </div>
      </div>
    </div>
  );
}
