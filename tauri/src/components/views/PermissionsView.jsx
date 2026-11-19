// src/components/views/PermissionsView.jsx - Safety Policy & Permission Tiers Workspace
import React, { useState, useEffect } from 'react';
import {
  Shield,
  RefreshCw,
  Search,
  ShieldCheck,
  ShieldAlert,
  ShieldX,
  Lock,
  User,
  AlertTriangle
} from 'lucide-react';
import { getPermissionsAudit } from '../../services/tauriBridge';
import '../../styles/SecondaryViews.css';

export default function PermissionsView() {
  const [audit, setAudit] = useState({ admin_rights: false, tools: [] });
  const [loading, setLoading] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [riskFilter, setRiskFilter] = useState(null);
  const [errorMsg, setErrorMsg] = useState(null);

  const fetchAudit = async () => {
    setLoading(true);
    setErrorMsg(null);
    try {
      const data = await getPermissionsAudit();
      setAudit(data || { admin_rights: false, tools: [] });
    } catch (err) {
      console.error('Failed to load permissions audit:', err);
      setErrorMsg('Could not retrieve permissions policy audit.');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchAudit();
  }, []);

  const tools = audit.tools || [];
  const isAdmin = Boolean(audit.admin_rights);

  const filteredTools = tools.filter((t) => {
    if (riskFilter && t.risk_level !== riskFilter) return false;
    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase();
      const matchName = (t.name || '').toLowerCase().includes(q);
      const matchCat = (t.category || '').toLowerCase().includes(q);
      const matchReason = (t.reason || '').toLowerCase().includes(q);
      if (!matchName && !matchCat && !matchReason) return false;
    }
    return true;
  });

  return (
    <div className="view-inner-container">
      {/* Header */}
      <div className="view-header-row">
        <div>
          <h2 className="view-title">Permissions & Security Policy</h2>
          <p className="view-subtitle">
            Centralized execution policy, three locked risk tiers, and audited tool enforcement rules.
          </p>
        </div>
        <button
          className="action-btn-secondary"
          onClick={fetchAudit}
          disabled={loading}
          type="button"
        >
          <RefreshCw size={14} className={loading ? 'spinning' : ''} />
          <span>Refresh</span>
        </button>
      </div>

      {errorMsg && (
        <div className="error-banner">
          <AlertTriangle size={15} />
          <span>{errorMsg}</span>
        </div>
      )}

      {/* Process Privilege Status Banner */}
      <div className={`permissions-privilege-banner ${isAdmin ? 'is-elevated' : 'is-standard'}`}>
        <div className="privilege-header">
          {isAdmin ? (
            <ShieldAlert size={18} className="text-warning" />
          ) : (
            <User size={18} className="text-primary" />
          )}
          <span className="privilege-title">
            Process Privilege Level:{' '}
            {isAdmin ? 'Elevated: Windows Administrator Rights' : 'Standard User Privilege (UAC Standard)'}
          </span>
        </div>
        <div className="privilege-description">
          <strong>Immutable Security Policy:</strong> Operating system administrator privileges do
          NOT bypass confirmation requirements or unblock high-risk actions. All actions strictly adhere
          to the locked permission tiers below.
        </div>
      </div>

      {/* 3 Locked Risk Classification Tiers */}
      <div className="risk-tiers-grid">
        <div className="risk-tier-card tier-safe">
          <div className="tier-header">
            <ShieldCheck size={18} className="text-green" />
            <span className="tier-name">Tier 1: SAFE</span>
          </div>
          <p className="tier-desc">
            Read-only queries, system metrics, clipboard reading, and non-destructive information retrieval.
          </p>
          <span className="tier-status-pill pill-allow">Execution: Direct / Immediate</span>
        </div>

        <div className="risk-tier-card tier-confirm">
          <div className="tier-header">
            <ShieldAlert size={18} className="text-orange" />
            <span className="tier-name">Tier 2: CONFIRMATION_REQUIRED</span>
          </div>
          <p className="tier-desc">
            Launching applications, modifying files, killing processes, and state-altering tool operations.
          </p>
          <span className="tier-status-pill pill-confirm">Execution: Requires User Approval</span>
        </div>

        <div className="risk-tier-card tier-highrisk">
          <div className="tier-header">
            <ShieldX size={18} className="text-red" />
            <span className="tier-name">Tier 3: HIGH_RISK</span>
          </div>
          <p className="tier-desc">
            System deletion, registry alteration, credential tampering, and arbitrary shell scripts.
          </p>
          <span className="tier-status-pill pill-deny">Execution: Permanently Denied</span>
        </div>
      </div>

      {/* Filter and Search Bar */}
      <div className="filter-search-bar">
        <div className="filter-pill-group">
          {[
            { label: 'All Tiers', value: null },
            { label: 'Safe', value: 'SAFE' },
            { label: 'Confirmation Required', value: 'CONFIRMATION_REQUIRED' },
            { label: 'High Risk', value: 'HIGH_RISK' }
          ].map((f) => (
            <button
              key={f.label}
              className={`filter-pill ${riskFilter === f.value ? 'is-active' : ''}`}
              onClick={() => setRiskFilter(f.value)}
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
            placeholder="Search audited tools..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
          />
        </div>
      </div>

      {/* Tool Audit Table */}
      <div className="feed-scroll-container">
        {loading && tools.length === 0 ? (
          <div className="feed-empty-state">
            <RefreshCw size={24} className="spinning text-orange" />
            <p>Auditing tool permissions...</p>
          </div>
        ) : filteredTools.length === 0 ? (
          <div className="feed-empty-state">
            <Shield size={28} className="text-muted" />
            <h3>No Tools Matched</h3>
            <p>Try adjusting your search query or risk tier filter.</p>
          </div>
        ) : (
          <div className="tools-audit-table">
            <div className="audit-table-header">
              <span className="col-name">Tool Name</span>
              <span className="col-cat">Category</span>
              <span className="col-risk">Risk Level</span>
              <span className="col-status">Enforcement Status</span>
              <span className="col-reason">Reasoning</span>
            </div>
            {filteredTools.map((t, idx) => (
              <div key={idx} className="audit-table-row">
                <span className="col-name">
                  <code>{t.name}</code>
                </span>
                <span className="col-cat">
                  <span className="category-tag-subtle">{t.category}</span>
                </span>
                <span className="col-risk">
                  <span className={`risk-tag is-${(t.risk_level || '').toLowerCase()}`}>
                    {t.risk_level}
                  </span>
                </span>
                <span className="col-status">
                  <span className={`status-badge is-${(t.status || '').toLowerCase()}`}>
                    {t.status}
                  </span>
                </span>
                <span className="col-reason">{t.reason || 'Standard policy rule'}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
