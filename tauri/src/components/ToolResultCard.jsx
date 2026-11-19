// src/components/ToolResultCard.jsx - Dedicated card for structured tool execution results
import React, { useState } from 'react';
import {
  Wrench,
  CheckCircle2,
  AlertCircle,
  Copy,
  Check,
  ChevronDown,
  ChevronUp,
  Terminal
} from 'lucide-react';

export default function ToolResultCard({
  toolName = 'Tool',
  status = 'SUCCESS', // 'SUCCESS' | 'FAILED' | 'RUNNING'
  stdout = '',
  error = null,
  commandArg = '',
  initialCollapsed = null
}) {
  const [copied, setCopied] = useState(false);

  const rawStdout = typeof stdout === 'string' ? stdout : (stdout ? JSON.stringify(stdout, null, 2) : '');
  const rawError = typeof error === 'string' ? error : (error ? JSON.stringify(error, null, 2) : '');
  const fullOutput = rawError ? (rawStdout ? `${rawStdout}\n\n[ERROR]:\n${rawError}` : rawError) : rawStdout;

  const lineCount = fullOutput.split('\n').length;
  // Auto-collapse if lines exceed 10 unless explicitly specified
  const shouldAutoCollapse = initialCollapsed !== null ? initialCollapsed : lineCount > 10;
  const [collapsed, setCollapsed] = useState(shouldAutoCollapse);

  const isSuccess = (status || '').toUpperCase() === 'SUCCESS';
  const isFailed = (status || '').toUpperCase() === 'FAILED' || Boolean(rawError);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(fullOutput);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.warn('Failed to copy tool output:', err);
    }
  };

  return (
    <div className={`tool-result-card ${isFailed ? 'is-failed' : 'is-success'}`}>
      {/* Top Header Bar */}
      <div className="tool-result-header">
        <div className="tool-result-title-group">
          <Terminal size={14} className="tool-result-icon" />
          <span className="tool-result-name">{toolName}</span>
          <span className={`tool-status-badge ${isFailed ? 'badge-failed' : 'badge-success'}`}>
            {isFailed ? <AlertCircle size={12} /> : <CheckCircle2 size={12} />}
            <span>{isFailed ? 'FAILED' : 'SUCCESS'}</span>
          </span>
          {commandArg && (
            <span className="tool-result-arg" title={commandArg}>
              {commandArg}
            </span>
          )}
        </div>

        <div className="tool-result-actions">
          <button
            className="tool-action-btn"
            onClick={handleCopy}
            type="button"
            title="Copy tool output"
          >
            {copied ? (
              <>
                <Check size={12} />
                <span>Copied</span>
              </>
            ) : (
              <>
                <Copy size={12} />
                <span>Copy</span>
              </>
            )}
          </button>

          {lineCount > 3 && (
            <button
              className="tool-action-btn tool-collapse-btn"
              onClick={() => setCollapsed(!collapsed)}
              type="button"
              title={collapsed ? 'Expand output' : 'Collapse output'}
            >
              {collapsed ? (
                <>
                  <span>Expand ({lineCount} lines)</span>
                  <ChevronDown size={13} />
                </>
              ) : (
                <>
                  <span>Collapse</span>
                  <ChevronUp size={13} />
                </>
              )}
            </button>
          )}
        </div>
      </div>

      {/* Output Content Body */}
      {!collapsed && (
        <div className="tool-result-body">
          {rawStdout && (
            <pre className="tool-result-stdout">
              <code>{rawStdout}</code>
            </pre>
          )}
          {rawError && (
            <div className="tool-result-error">
              <span className="error-label">Execution Error:</span>
              <pre className="tool-result-stderr">
                <code>{rawError}</code>
              </pre>
            </div>
          )}
          {!rawStdout && !rawError && (
            <div className="tool-result-empty">
              <em>(Tool produced no textual output)</em>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
