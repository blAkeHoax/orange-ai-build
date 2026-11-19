// src/components/views/AgentJobsView.jsx - Fully Functional Agent Jobs Workspace
import React, { useState, useEffect } from 'react';
import {
  Zap,
  RefreshCw,
  Search,
  Play,
  Pause,
  XCircle,
  CheckCircle2,
  Clock,
  AlertCircle,
  ChevronDown,
  ChevronRight,
  ShieldAlert
} from 'lucide-react';
import {
  getJobs,
  getJobHistory,
  pauseJob,
  resumeJob,
  cancelJob,
  confirmJob
} from '../../services/tauriBridge';
import '../../styles/SecondaryViews.css';

const STATUS_FILTERS = [
  { label: 'All', value: null },
  { label: 'Active', value: 'RUNNING' },
  { label: 'Paused', value: 'PAUSED' },
  { label: 'Pending', value: 'PENDING' },
  { label: 'Waiting', value: 'WAITING_CONFIRMATION' },
  { label: 'Completed', value: 'COMPLETED' },
  { label: 'Failed', value: 'FAILED' }
];

export default function AgentJobsView() {
  const [jobs, setJobs] = useState([]);
  const [loading, setLoading] = useState(false);
  const [filterStatus, setFilterStatus] = useState(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [expandedJobId, setExpandedJobId] = useState(null);
  const [jobHistory, setJobHistory] = useState({});
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [actionError, setActionError] = useState(null);

  const fetchJobsList = async () => {
    setLoading(true);
    setActionError(null);
    try {
      const list = await getJobs(filterStatus);
      setJobs(Array.isArray(list) ? list : []);
    } catch (err) {
      console.error('Failed to load jobs:', err);
      setActionError('Could not retrieve jobs from Python JobManager.');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchJobsList();
  }, [filterStatus]);

  const toggleHistory = async (jobId) => {
    if (expandedJobId === jobId) {
      setExpandedJobId(null);
      return;
    }
    setExpandedJobId(jobId);
    if (!jobHistory[jobId]) {
      setLoadingHistory(true);
      try {
        const steps = await getJobHistory(jobId);
        setJobHistory((prev) => ({ ...prev, [jobId]: Array.isArray(steps) ? steps : [] }));
      } catch (err) {
        console.error('Error fetching job history:', err);
      } finally {
        setLoadingHistory(false);
      }
    }
  };

  const handlePause = async (jobId) => {
    try {
      await pauseJob(jobId);
      await fetchJobsList();
    } catch (err) {
      setActionError(`Failed to pause job: ${err.message || String(err)}`);
    }
  };

  const handleResume = async (jobId) => {
    try {
      await resumeJob(jobId);
      await fetchJobsList();
    } catch (err) {
      setActionError(`Failed to resume job: ${err.message || String(err)}`);
    }
  };

  const handleCancel = async (jobId) => {
    try {
      await cancelJob(jobId);
      await fetchJobsList();
    } catch (err) {
      setActionError(`Failed to cancel job: ${err.message || String(err)}`);
    }
  };

  const handleConfirm = async (jobId, approved) => {
    try {
      await confirmJob(jobId, approved);
      await fetchJobsList();
    } catch (err) {
      setActionError(`Failed to confirm job: ${err.message || String(err)}`);
    }
  };

  const filteredJobs = jobs.filter((j) => {
    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase();
      const matchGoal = (j.goal || '').toLowerCase().includes(q);
      const matchId = (j.short_id || j.id || '').toLowerCase().includes(q);
      if (!matchGoal && !matchId) return false;
    }
    return true;
  });

  return (
    <div className="view-inner-container">
      {/* View Header */}
      <div className="view-header-row">
        <div>
          <h2 className="view-title">Agent Jobs</h2>
          <p className="view-subtitle">
            Inspect autonomous scheduled tasks, step audit trails, and execution states.
          </p>
        </div>
        <button
          className="action-btn-secondary"
          onClick={fetchJobsList}
          disabled={loading}
          type="button"
        >
          <RefreshCw size={14} className={loading ? 'spinning' : ''} />
          <span>Refresh</span>
        </button>
      </div>

      {actionError && (
        <div className="error-banner">
          <AlertCircle size={15} />
          <span>{actionError}</span>
        </div>
      )}

      {/* Filter and Search Bar */}
      <div className="filter-search-bar">
        <div className="filter-pill-group">
          {STATUS_FILTERS.map((f) => (
            <button
              key={f.label}
              className={`filter-pill ${filterStatus === f.value ? 'is-active' : ''}`}
              onClick={() => setFilterStatus(f.value)}
              type="button"
            >
              {f.label}
            </button>
          ))}
        </div>
        <div className="search-input-wrapper">
          <Search size={14} className="search-icon" />
          <input
            type="text"
            className="search-input"
            placeholder="Search by goal or ID..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
          />
        </div>
      </div>

      {/* Jobs Feed */}
      <div className="feed-scroll-container">
        {loading && jobs.length === 0 ? (
          <div className="feed-empty-state">
            <RefreshCw size={24} className="spinning text-orange" />
            <p>Loading agent jobs...</p>
          </div>
        ) : filteredJobs.length === 0 ? (
          <div className="feed-empty-state">
            <Zap size={28} className="text-muted" />
            <h3>No Agent Jobs Found</h3>
            <p>
              {searchQuery || filterStatus
                ? 'No jobs match your current filter or search criteria.'
                : 'Autonomous background tasks and agent workflows will appear here.'}
            </p>
          </div>
        ) : (
          filteredJobs.map((j) => {
            const isExpanded = expandedJobId === j.id;
            const steps = jobHistory[j.id] || [];
            const isWaiting = j.status === 'WAITING_CONFIRMATION';

            return (
              <div key={j.id} className="job-card">
                <div className="job-card-main">
                  <div className="job-card-header">
                    <span className="job-id-tag">#{j.short_id || j.id.slice(0, 8)}</span>
                    <span className={`status-tag is-${(j.status || '').toLowerCase()}`}>
                      {j.status}
                    </span>
                    <span className="step-count-tag">
                      Steps: {j.step_count ?? 0}
                    </span>
                  </div>

                  <div className="job-goal-text">{j.goal}</div>

                  {j.last_error && (
                    <div className="job-error-text">
                      <AlertCircle size={13} />
                      <span>{j.last_error}</span>
                    </div>
                  )}

                  {isWaiting && j.pending_tool && (
                    <div className="job-confirmation-alert">
                      <ShieldAlert size={14} />
                      <span>
                        Tool <strong>{j.pending_tool}</strong> requires authorization.
                      </span>
                      <div className="job-confirm-btns">
                        <button
                          className="btn-tiny-confirm"
                          onClick={() => handleConfirm(j.id, true)}
                          type="button"
                        >
                          Approve
                        </button>
                        <button
                          className="btn-tiny-deny"
                          onClick={() => handleConfirm(j.id, false)}
                          type="button"
                        >
                          Deny
                        </button>
                      </div>
                    </div>
                  )}

                  <div className="job-card-actions">
                    <button
                      className="btn-expand-steps"
                      onClick={() => toggleHistory(j.id)}
                      type="button"
                    >
                      {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                      <span>{isExpanded ? 'Hide Steps' : 'View Audit Trail'}</span>
                    </button>

                    <div className="job-lifecycle-controls">
                      {j.status === 'RUNNING' && (
                        <button
                          className="btn-ctrl-pause"
                          onClick={() => handlePause(j.id)}
                          title="Pause Job"
                          type="button"
                        >
                          <Pause size={13} />
                          <span>Pause</span>
                        </button>
                      )}
                      {j.status === 'PAUSED' && (
                        <button
                          className="btn-ctrl-resume"
                          onClick={() => handleResume(j.id)}
                          title="Resume Job"
                          type="button"
                        >
                          <Play size={13} />
                          <span>Resume</span>
                        </button>
                      )}
                      {['RUNNING', 'PAUSED', 'PENDING', 'WAITING_CONFIRMATION'].includes(j.status) && (
                        <button
                          className="btn-ctrl-cancel"
                          onClick={() => handleCancel(j.id)}
                          title="Cancel Job"
                          type="button"
                        >
                          <XCircle size={13} />
                          <span>Cancel</span>
                        </button>
                      )}
                    </div>
                  </div>
                </div>

                {/* Expanded Execution Audit Trail */}
                {isExpanded && (
                  <div className="job-steps-expanded">
                    <div className="job-steps-title">Execution Steps:</div>
                    {loadingHistory && steps.length === 0 ? (
                      <div className="steps-loading">Loading steps...</div>
                    ) : steps.length === 0 ? (
                      <div className="steps-empty">No execution steps recorded yet.</div>
                    ) : (
                      <div className="steps-timeline-list">
                        {steps.map((st, sIdx) => (
                          <div key={sIdx} className={`step-item is-${st.success ? 'success' : 'fail'}`}>
                            <div className="step-item-meta">
                              <span className="step-tool">{st.tool_name || st.action_type || 'Reasoning'}</span>
                              <span className="step-duration">{st.duration_ms ? `${st.duration_ms}ms` : ''}</span>
                              <span className="step-time">{st.started_at ? st.started_at.slice(11, 19) : ''}</span>
                            </div>
                            {st.reasoning && <div className="step-reasoning">{st.reasoning}</div>}
                            {st.error && <div className="step-error">{st.error}</div>}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
