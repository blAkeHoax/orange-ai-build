// src/components/views/ActivityView.jsx - Historical and Live Activity & Audit Trail Workspace
import React, { useState, useEffect } from 'react';
import {
  Activity,
  RefreshCw,
  Search,
  FileText,
  Zap,
  MessageSquare,
  AlertCircle,
  CheckCircle2,
  Clock,
  GitBranch,
  GitCommit,
  FolderGit2
} from 'lucide-react';
import { getActivity, getLatestReconciliations, onEvent } from '../../services/tauriBridge';
import '../../styles/SecondaryViews.css';

const SOURCES = [
  { id: 'logs', label: 'System Logs', icon: FileText, provenance: 'orange_ai.log' },
  { id: 'jobs', label: 'Job Audits', icon: Zap, provenance: 'JobStore database' },
  { id: 'conversations', label: 'Conversations', icon: MessageSquare, provenance: 'conversation_history' },
  { id: 'reconciliations', label: 'Reconciliations', icon: GitBranch, provenance: 'project_reconciliations' }
];

export default function ActivityView() {
  const [activeSource, setActiveSource] = useState('logs');
  const [levelFilter, setLevelFilter] = useState(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [activityData, setActivityData] = useState({ logs: [], job_steps: [], conversations: [] });
  const [reconciliations, setReconciliations] = useState([]);
  const [loading, setLoading] = useState(false);
  const [errorMsg, setErrorMsg] = useState(null);

  const fetchActivity = async () => {
    setLoading(true);
    setErrorMsg(null);
    try {
      const [data, reconData] = await Promise.all([
        getActivity(),
        getLatestReconciliations()
      ]);
      setActivityData(data || { logs: [], job_steps: [], conversations: [] });
      setReconciliations(Array.isArray(reconData) ? reconData : []);
    } catch (err) {
      console.error('Failed to load activity:', err);
      setErrorMsg('Could not retrieve activity records.');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchActivity();

    let unlisten = () => {};
    async function subscribeRecon() {
      unlisten = await onEvent('reconciliation_ready', () => {
        getLatestReconciliations().then((res) => {
          if (Array.isArray(res)) setReconciliations(res);
        }).catch(() => {});
      });
    }
    subscribeRecon();

    return () => {
      if (typeof unlisten === 'function') unlisten();
    };
  }, []);

  const currentSourceMeta = SOURCES.find((s) => s.id === activeSource) || SOURCES[0];

  // Filtering
  const filteredLogs = (activityData.logs || []).filter((item) => {
    if (levelFilter && item.level !== levelFilter) return false;
    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase();
      return (item.message || '').toLowerCase().includes(q) || (item.level || '').toLowerCase().includes(q);
    }
    return true;
  });

  const filteredJobSteps = (activityData.job_steps || []).filter((item) => {
    if (levelFilter === 'SUCCESS' && !item.success) return false;
    if (levelFilter === 'FAILED' && item.success) return false;
    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase();
      const matchGoal = (item.goal || '').toLowerCase().includes(q);
      const matchTool = (item.tool_name || '').toLowerCase().includes(q);
      const matchReason = (item.reasoning || '').toLowerCase().includes(q);
      if (!matchGoal && !matchTool && !matchReason) return false;
    }
    return true;
  });

  const filteredConversations = (activityData.conversations || []).filter((item) => {
    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase();
      return (item.content || '').toLowerCase().includes(q) || (item.role || '').toLowerCase().includes(q);
    }
    return true;
  });

  const filteredReconciliations = (reconciliations || []).filter((item) => {
    if (levelFilter && item.status?.toUpperCase() !== levelFilter) return false;
    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase();
      return (
        (item.workspace_name || '').toLowerCase().includes(q) ||
        (item.workspace_path || '').toLowerCase().includes(q) ||
        (item.git_branch || '').toLowerCase().includes(q) ||
        (item.status || '').toLowerCase().includes(q)
      );
    }
    return true;
  });

  return (
    <div className="view-inner-container">
      {/* Header */}
      <div className="view-header-row">
        <div>
          <h2 className="view-title">System Activity & Audit Trail</h2>
          <p className="view-subtitle">
            Inspect persistent diagnostic logs, autonomous job execution steps, and conversation turns.
          </p>
        </div>
        <button
          className="action-btn-secondary"
          onClick={fetchActivity}
          disabled={loading}
          type="button"
        >
          <RefreshCw size={14} className={loading ? 'spinning' : ''} />
          <span>Refresh</span>
        </button>
      </div>

      {errorMsg && (
        <div className="error-banner">
          <AlertCircle size={15} />
          <span>{errorMsg}</span>
        </div>
      )}

      {/* Source Selector Bar (Tabs) */}
      <div className="activity-tabs-bar">
        {SOURCES.map((s) => {
          const Icon = s.icon;
          const isActive = activeSource === s.id;
          return (
            <button
              key={s.id}
              className={`activity-tab-btn ${isActive ? 'is-active' : ''}`}
              onClick={() => {
                setActiveSource(s.id);
                setLevelFilter(null);
              }}
              type="button"
            >
              <Icon size={14} />
              <span>{s.label}</span>
            </button>
          );
        })}
      </div>

      {/* Provenance & Sub-filter Banner */}
      <div className="activity-provenance-banner">
        <span className="provenance-text">
          Source: <code>{currentSourceMeta.provenance}</code>
        </span>

        {activeSource === 'logs' && (
          <div className="sub-filter-group">
            {['ALL', 'INFO', 'WARNING', 'ERROR'].map((lvl) => {
              const val = lvl === 'ALL' ? null : lvl;
              return (
                <button
                  key={lvl}
                  className={`sub-filter-btn ${levelFilter === val ? 'is-active' : ''}`}
                  onClick={() => setLevelFilter(val)}
                  type="button"
                >
                  {lvl}
                </button>
              );
            })}
          </div>
        )}

        {activeSource === 'jobs' && (
          <div className="sub-filter-group">
            {['ALL', 'SUCCESS', 'FAILED'].map((lvl) => {
              const val = lvl === 'ALL' ? null : lvl;
              return (
                <button
                  key={lvl}
                  className={`sub-filter-btn ${levelFilter === val ? 'is-active' : ''}`}
                  onClick={() => setLevelFilter(val)}
                  type="button"
                >
                  {lvl}
                </button>
              );
            })}
          </div>
        )}
      </div>

      {/* Search Input Bar */}
      <div className="filter-search-bar">
        <div className="search-input-wrapper is-full-width">
          <Search size={14} className="search-icon" />
          <input
            type="text"
            className="search-input"
            placeholder={`Search ${currentSourceMeta.label.toLowerCase()}...`}
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
          />
        </div>
      </div>

      {/* Feed Area */}
      <div className="feed-scroll-container">
        {loading && (
          <div className="feed-empty-state">
            <RefreshCw size={24} className="spinning text-orange" />
            <p>Loading activity records...</p>
          </div>
        )}

        {/* 1. Logs Feed */}
        {!loading && activeSource === 'logs' && (
          filteredLogs.length === 0 ? (
            <div className="feed-empty-state">
              <FileText size={28} className="text-muted" />
              <h3>No Log Entries Found</h3>
              <p>No log records match your filter criteria.</p>
            </div>
          ) : (
            filteredLogs.map((item, idx) => (
              <div key={idx} className={`activity-row is-log log-${(item.level || '').toLowerCase()}`}>
                <div className="activity-row-meta">
                  <span className={`log-badge badge-${(item.level || '').toLowerCase()}`}>
                    {item.level}
                  </span>
                  <span className="activity-time">{item.time}</span>
                </div>
                <div className="activity-message-text">{item.message}</div>
              </div>
            ))
          )
        )}

        {/* 2. Job Steps Feed */}
        {!loading && activeSource === 'jobs' && (
          filteredJobSteps.length === 0 ? (
            <div className="feed-empty-state">
              <Zap size={28} className="text-muted" />
              <h3>No Job Steps Recorded</h3>
              <p>Autonomous jobs will record step execution audits here.</p>
            </div>
          ) : (
            filteredJobSteps.map((item, idx) => (
              <div key={idx} className={`activity-row is-step ${item.success ? 'is-success' : 'is-fail'}`}>
                <div className="activity-row-meta">
                  <span className="activity-job-id">#{item.job_id}</span>
                  <span className="activity-tool-name">{item.tool_name || item.action_type || 'Reasoning'}</span>
                  <span className={`status-pill-small ${item.success ? 'pill-success' : 'pill-fail'}`}>
                    {item.success ? 'SUCCESS' : 'FAILED'}
                  </span>
                  {item.duration_ms ? <span className="activity-duration">{item.duration_ms}ms</span> : null}
                  <span className="activity-time">{item.time}</span>
                </div>
                <div className="activity-goal-text">Goal: {item.goal}</div>
                {item.reasoning && <div className="activity-reasoning-text">{item.reasoning}</div>}
                {item.error && <div className="activity-error-text">Error: {item.error}</div>}
              </div>
            ))
          )
        )}

        {/* 3. Conversations Feed */}
        {!loading && activeSource === 'conversations' && (
          filteredConversations.length === 0 ? (
            <div className="feed-empty-state">
              <MessageSquare size={28} className="text-muted" />
              <h3>No Conversation Turns Found</h3>
              <p>Recent conversation turns from persistent history will appear here.</p>
            </div>
          ) : (
            filteredConversations.map((item, idx) => (
              <div key={idx} className={`activity-row is-conv role-${item.role}`}>
                <div className="activity-row-meta">
                  <span className={`role-badge role-${item.role}`}>
                    {item.role === 'user' ? 'YOU' : 'ORANGE AI'}
                  </span>
                  <span className="activity-time">{item.time}</span>
                </div>
                <div className="activity-message-text">{item.content}</div>
              </div>
            ))
          )
        )}

        {/* 4. Project Reconciliations Feed */}
        {!loading && activeSource === 'reconciliations' && (
          filteredReconciliations.length === 0 ? (
            <div className="feed-empty-state">
              <GitBranch size={28} className="text-muted" />
              <h3>No Workspace Reconciliations Found</h3>
              <p>Startup and on-demand project reconciliations will appear here.</p>
            </div>
          ) : (
            filteredReconciliations.map((item, idx) => {
              const statusClass = (item.status || 'clean').toLowerCase();
              const newCommits = Array.isArray(item.new_commits) ? item.new_commits : [];
              const changedFiles = Array.isArray(item.changed_files) ? item.changed_files : [];
              const uncommitted = Array.isArray(item.uncommitted_changes) ? item.uncommitted_changes : [];
              const outstanding = Array.isArray(item.outstanding_items) ? item.outstanding_items : [];

              return (
                <div key={item.id || idx} className={`activity-row is-recon status-${statusClass}`}>
                  <div className="activity-row-meta">
                    <span className="activity-job-id">{item.workspace_name}</span>
                    <span className={`status-pill-small pill-${statusClass}`}>
                      {(item.status || 'CLEAN').toUpperCase()}
                    </span>
                    {item.git_branch && (
                      <span className="activity-tool-name">
                        branch: {item.git_branch}
                      </span>
                    )}
                    {item.git_head_commit && (
                      <span className="activity-duration">
                        commit: {String(item.git_head_commit).slice(0, 7)}
                      </span>
                    )}
                    <span className="activity-time">
                      {item.reconciled_at ? new Date(item.reconciled_at).toLocaleString() : ''}
                    </span>
                  </div>

                  <div className="activity-goal-text">
                    <strong>Workspace:</strong> {item.workspace_path}
                  </div>

                  {newCommits.length > 0 && (
                    <div className="activity-reasoning-text">
                      <strong>New Commits ({newCommits.length}):</strong>
                      <ul style={{ margin: '4px 0 0 16px', padding: 0 }}>
                        {newCommits.slice(0, 5).map((c, cIdx) => (
                          <li key={cIdx}>{typeof c === 'object' ? `${c.hash?.slice(0, 7)}: ${c.subject}` : String(c)}</li>
                        ))}
                      </ul>
                    </div>
                  )}

                  {changedFiles.length > 0 && (
                    <div className="activity-message-text" style={{ fontSize: '11px', color: 'var(--text-secondary)' }}>
                      <strong>Changed Files ({changedFiles.length}):</strong> {changedFiles.slice(0, 8).join(', ')}{changedFiles.length > 8 ? '...' : ''}
                    </div>
                  )}

                  {uncommitted.length > 0 && (
                    <div className="activity-error-text" style={{ fontSize: '11px' }}>
                      <strong>Uncommitted Working Tree Changes ({uncommitted.length}):</strong> {uncommitted.slice(0, 5).join(', ')}
                    </div>
                  )}

                  {outstanding.length > 0 && (
                    <div className="activity-reasoning-text" style={{ fontSize: '11px' }}>
                      <strong>Outstanding Items ({outstanding.length}):</strong> {outstanding.map(o => o.summary).join(' | ')}
                    </div>
                  )}
                </div>
              );
            })
          )
        )}
      </div>
    </div>
  );
}
