// src/components/views/MemoryView.jsx - Memory 2.0 Inspection & Management Workspace
import React, { useState, useEffect } from 'react';
import {
  Brain,
  RefreshCw,
  Search,
  Plus,
  Trash2,
  AlertTriangle,
  CheckCircle2,
  X
} from 'lucide-react';
import {
  getMemoryStats,
  getStructuredMemory,
  getLegacyMemory,
  retractMemory,
  forgetMemory,
  rememberFact
} from '../../services/tauriBridge';
import '../../styles/SecondaryViews.css';

const CATEGORIES = [
  { label: 'All', value: null },
  { label: 'Profile', value: 'profile' },
  { label: 'Projects', value: 'project' },
  { label: 'Tools', value: 'tool' },
  { label: 'Preferences', value: 'preference' },
  { label: 'Facts', value: 'fact' },
  { label: 'Legacy', value: 'legacy' }
];

export default function MemoryView() {
  const [stats, setStats] = useState(null);
  const [structuredMemories, setStructuredMemories] = useState([]);
  const [legacyMemories, setLegacyMemories] = useState([]);
  const [activeCategory, setActiveCategory] = useState(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [loading, setLoading] = useState(false);
  const [showAddForm, setShowAddForm] = useState(false);
  const [newFact, setNewFact] = useState('');
  const [savingFact, setSavingFact] = useState(false);
  const [confirmTarget, setConfirmTarget] = useState(null); // { type, item }
  const [errorMsg, setErrorMsg] = useState(null);

  const fetchAllMemoryData = async () => {
    setLoading(true);
    setErrorMsg(null);
    try {
      const [sData, structData, legData] = await Promise.all([
        getMemoryStats(),
        getStructuredMemory(),
        getLegacyMemory(80)
      ]);
      setStats(sData || {});
      setStructuredMemories(Array.isArray(structData) ? structData : []);
      setLegacyMemories(Array.isArray(legData) ? legData : []);
    } catch (err) {
      console.error('Failed to load memory data:', err);
      setErrorMsg('Error loading persistent memory database.');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchAllMemoryData();
  }, []);

  const handleSaveFact = async (e) => {
    e.preventDefault();
    if (!newFact.trim() || savingFact) return;
    setSavingFact(true);
    try {
      const res = await rememberFact(newFact.trim());
      if (res?.success) {
        setNewFact('');
        setShowAddForm(false);
        await fetchAllMemoryData();
      } else {
        setErrorMsg(res?.error || 'Failed to save fact.');
      }
    } catch (err) {
      setErrorMsg(`Failed to save memory: ${err.message || String(err)}`);
    } finally {
      setSavingFact(false);
    }
  };

  const handleRetractStructured = async (mem) => {
    try {
      await retractMemory(mem.category, mem.key);
      setConfirmTarget(null);
      await fetchAllMemoryData();
    } catch (err) {
      setErrorMsg(`Failed to retract memory: ${err.message || String(err)}`);
    }
  };

  const handleForgetLegacy = async (text) => {
    try {
      await forgetMemory(text.slice(0, 30));
      setConfirmTarget(null);
      await fetchAllMemoryData();
    } catch (err) {
      setErrorMsg(`Failed to forget memory: ${err.message || String(err)}`);
    }
  };

  // Filter memories
  const filteredStructured = structuredMemories.filter((m) => {
    if (activeCategory && activeCategory !== 'legacy') {
      if ((m.category || '').toLowerCase() !== activeCategory.toLowerCase()) return false;
    }
    if (activeCategory === 'legacy') return false;
    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase();
      const matchKey = (m.key || '').toLowerCase().includes(q);
      const matchVal = (m.value || '').toLowerCase().includes(q);
      const matchCat = (m.category || '').toLowerCase().includes(q);
      if (!matchKey && !matchVal && !matchCat) return false;
    }
    return true;
  });

  const filteredLegacy = legacyMemories.filter((text) => {
    if (activeCategory && activeCategory !== 'legacy') return false;
    if (searchQuery.trim()) {
      return text.toLowerCase().includes(searchQuery.toLowerCase());
    }
    return true;
  });

  return (
    <div className="view-inner-container">
      {/* Header */}
      <div className="view-header-row">
        <div>
          <h2 className="view-title">Contextual Memory</h2>
          <p className="view-subtitle">
            Browse, search, and manage persistent profile facts, project context, and knowledge.
          </p>
        </div>
        <div className="header-actions-group">
          <button
            className="action-btn-primary"
            onClick={() => setShowAddForm((prev) => !prev)}
            type="button"
          >
            <Plus size={14} />
            <span>Remember Fact</span>
          </button>
          <button
            className="action-btn-secondary"
            onClick={fetchAllMemoryData}
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

      {/* Stats Summary Cards */}
      <div className="memory-stats-grid">
        <div className="stat-card">
          <span className="stat-label">Structured Memories</span>
          <span className="stat-value">{stats?.structured_memories ?? stats?.structured_count ?? structuredMemories.length}</span>
        </div>
        <div className="stat-card">
          <span className="stat-label">Legacy Facts</span>
          <span className="stat-value">{stats?.legacy_memories ?? stats?.legacy_count ?? legacyMemories.length}</span>
        </div>
        <div className="stat-card">
          <span className="stat-label">Total Persistent Memories</span>
          <span className="stat-value">
            {stats?.total ?? ((stats?.structured_memories ?? stats?.structured_count ?? structuredMemories.length) + (stats?.legacy_memories ?? stats?.legacy_count ?? legacyMemories.length))}
          </span>
        </div>
      </div>

      {/* Inline Add Fact Form */}
      {showAddForm && (
        <form className="memory-add-form" onSubmit={handleSaveFact}>
          <div className="form-title">Store New Contextual Fact:</div>
          <div className="form-input-row">
            <input
              type="text"
              className="form-text-input"
              placeholder="e.g. My preferred terminal is PowerShell on Windows"
              value={newFact}
              onChange={(e) => setNewFact(e.target.value)}
              autoFocus
            />
            <button className="btn-save-fact" type="submit" disabled={savingFact || !newFact.trim()}>
              {savingFact ? 'Saving...' : 'Save Fact'}
            </button>
            <button
              className="btn-cancel-fact"
              type="button"
              onClick={() => {
                setShowAddForm(false);
                setNewFact('');
              }}
            >
              Cancel
            </button>
          </div>
        </form>
      )}

      {/* Confirmation Dialog for Retraction */}
      {confirmTarget && (
        <div className="memory-confirm-dialog">
          <AlertTriangle size={16} className="text-orange" />
          <div className="confirm-text">
            Permanent Retraction:{' '}
            {confirmTarget.type === 'structured'
              ? `Retract [${confirmTarget.item.category.toUpperCase()}] "${confirmTarget.item.key}"?`
              : `Forget legacy fact "${confirmTarget.item.slice(0, 40)}..."?`}
          </div>
          <div className="confirm-buttons">
            <button
              className="btn-danger-confirm"
              onClick={() =>
                confirmTarget.type === 'structured'
                  ? handleRetractStructured(confirmTarget.item)
                  : handleForgetLegacy(confirmTarget.item)
              }
              type="button"
            >
              Confirm Retraction
            </button>
            <button
              className="btn-cancel-confirm"
              onClick={() => setConfirmTarget(null)}
              type="button"
            >
              Cancel
            </button>
          </div>
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
            placeholder="Search keys, values, or content..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
          />
        </div>
      </div>

      {/* Memory Cards Feed */}
      <div className="feed-scroll-container">
        {loading && structuredMemories.length === 0 && legacyMemories.length === 0 ? (
          <div className="feed-empty-state">
            <RefreshCw size={24} className="spinning text-orange" />
            <p>Loading persistent memories...</p>
          </div>
        ) : filteredStructured.length === 0 && filteredLegacy.length === 0 ? (
          <div className="feed-empty-state">
            <Brain size={28} className="text-muted" />
            <h3>No Memories Found</h3>
            <p>
              {searchQuery || activeCategory
                ? 'No memories match your search or category filter.'
                : 'No stored facts in persistent memory yet.'}
            </p>
          </div>
        ) : (
          <>
            {/* Structured Memories */}
            {filteredStructured.map((m, idx) => (
              <div key={`struct-${idx}`} className="memory-card">
                <div className="memory-card-header">
                  <span className={`category-tag is-${(m.category || '').toLowerCase()}`}>
                    {m.category}
                  </span>
                  <span className="memory-key-text">{m.key}</span>
                  <button
                    className="btn-retract"
                    onClick={() => setConfirmTarget({ type: 'structured', item: m })}
                    title="Retract Fact"
                    type="button"
                  >
                    <Trash2 size={13} />
                    <span>Retract</span>
                  </button>
                </div>
                <div className="memory-value-text">{m.value}</div>
              </div>
            ))}

            {/* Legacy Memories */}
            {filteredLegacy.map((text, idx) => (
              <div key={`leg-${idx}`} className="memory-card is-legacy">
                <div className="memory-card-header">
                  <span className="category-tag is-legacy">legacy</span>
                  <button
                    className="btn-retract"
                    onClick={() => setConfirmTarget({ type: 'legacy', item: text })}
                    title="Forget Fact"
                    type="button"
                  >
                    <Trash2 size={13} />
                    <span>Forget</span>
                  </button>
                </div>
                <div className="memory-value-text">{text}</div>
              </div>
            ))}
          </>
        )}
      </div>
    </div>
  );
}
