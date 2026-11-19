// src/components/views/ToolsView.jsx - Tool Registry & Capabilities Catalog Workspace
import React, { useState, useEffect } from 'react';
import {
  Wrench,
  RefreshCw,
  Search,
  MessageSquare,
  ShieldCheck,
  ShieldAlert,
  Folder,
  Cpu,
  Monitor,
  Terminal,
  AlertTriangle
} from 'lucide-react';
import { getTools } from '../../services/tauriBridge';
import '../../styles/SecondaryViews.css';

const CATEGORIES = [
  { label: 'All', value: null },
  { label: 'System', value: 'system' },
  { label: 'Files', value: 'files' },
  { label: 'Processes', value: 'processes' },
  { label: 'Apps', value: 'apps' }
];

export default function ToolsView({ onRunInChat }) {
  const [tools, setTools] = useState([]);
  const [loading, setLoading] = useState(false);
  const [activeCategory, setActiveCategory] = useState(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [errorMsg, setErrorMsg] = useState(null);

  const fetchToolsList = async () => {
    setLoading(true);
    setErrorMsg(null);
    try {
      const list = await getTools();
      setTools(Array.isArray(list) ? list : []);
    } catch (err) {
      console.error('Failed to load tools:', err);
      setErrorMsg('Could not load tool registry.');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchToolsList();
  }, []);

  const filteredTools = tools.filter((t) => {
    if (activeCategory && (t.category || '').toLowerCase() !== activeCategory.toLowerCase()) return false;
    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase();
      const matchName = (t.name || '').toLowerCase().includes(q);
      const matchDesc = (t.description || '').toLowerCase().includes(q);
      const matchCat = (t.category || '').toLowerCase().includes(q);
      if (!matchName && !matchDesc && !matchCat) return false;
    }
    return true;
  });

  const getCategoryIcon = (cat) => {
    switch ((cat || '').toLowerCase()) {
      case 'system':
        return <Cpu size={14} />;
      case 'files':
        return <Folder size={14} />;
      case 'processes':
        return <Terminal size={14} />;
      case 'apps':
        return <Monitor size={14} />;
      default:
        return <Wrench size={14} />;
    }
  };

  return (
    <div className="view-inner-container">
      {/* Header */}
      <div className="view-header-row">
        <div>
          <h2 className="view-title">Tools Catalog</h2>
          <p className="view-subtitle">
            Audited system, file, process, and application capabilities registered in the central registry.
          </p>
        </div>
        <div className="header-actions-group">
          <span className="count-badge">{tools.length} Tools Registered</span>
          <button
            className="action-btn-secondary"
            onClick={fetchToolsList}
            disabled={loading}
            type="button"
          >
            <RefreshCw size={14} className={loading ? 'spinning' : ''} />
            <span>Refresh</span>
          </button>
        </div>
      </div>

      {errorMsg && (
        <div className="error-banner">
          <AlertTriangle size={15} />
          <span>{errorMsg}</span>
        </div>
      )}

      {/* Filter and Search Bar */}
      <div className="filter-search-bar">
        <div className="filter-pill-group">
          {CATEGORIES.map((c) => (
            <button
              key={c.label}
              className={`filter-pill ${activeCategory === c.value ? 'is-active' : ''}`}
              onClick={() => setActiveCategory(c.value)}
              type="button"
            >
              {c.label}
            </button>
          ))}
        </div>
        <div className="search-input-wrapper">
          <Search size={14} className="search-icon" />
          <input
            type="text"
            className="search-input"
            placeholder="Search tools by name or description..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
          />
        </div>
      </div>

      {/* Tools Feed */}
      <div className="feed-scroll-container">
        {loading && tools.length === 0 ? (
          <div className="feed-empty-state">
            <RefreshCw size={24} className="spinning text-orange" />
            <p>Loading tools catalog...</p>
          </div>
        ) : filteredTools.length === 0 ? (
          <div className="feed-empty-state">
            <Wrench size={28} className="text-muted" />
            <h3>No Tools Matched</h3>
            <p>Try adjusting your category filter or search keywords.</p>
          </div>
        ) : (
          <div className="tools-cards-grid">
            {filteredTools.map((t, idx) => {
              const isConfirm = t.risk_level === 'CONFIRMATION_REQUIRED';
              return (
                <div key={idx} className="tool-catalog-card">
                  <div className="tool-card-top">
                    <div className="tool-cat-icon">{getCategoryIcon(t.category)}</div>
                    <span className="tool-title-name">{t.name}</span>
                    <span className={`risk-tag is-${(t.risk_level || '').toLowerCase()}`}>
                      {isConfirm ? <ShieldAlert size={12} /> : <ShieldCheck size={12} />}
                      <span>{isConfirm ? 'Confirm' : 'Safe'}</span>
                    </span>
                  </div>

                  <p className="tool-card-desc">{t.description || 'No description provided.'}</p>

                  <div className="tool-card-footer">
                    <span className="tool-cat-label">Category: {t.category}</span>
                    {onRunInChat && (
                      <button
                        className="btn-run-in-chat"
                        onClick={() => onRunInChat(`Run tool ${t.name}`)}
                        type="button"
                      >
                        <MessageSquare size={12} />
                        <span>Use in Chat</span>
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
