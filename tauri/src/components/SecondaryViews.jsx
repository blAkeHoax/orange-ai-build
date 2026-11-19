// src/components/SecondaryViews.jsx - Unified Secondary Workspace Container
import React from 'react';
import {
  Zap,
  Brain,
  Activity,
  Shield,
  Wrench,
  Settings
} from 'lucide-react';
import BrandTrigger from './BrandTrigger';
import AgentJobsView from './views/AgentJobsView';
import MemoryView from './views/MemoryView';
import ActivityView from './views/ActivityView';
import PermissionsView from './views/PermissionsView';
import ToolsView from './views/ToolsView';
import SettingsView from './views/SettingsView';
import '../styles/SecondaryViews.css';

const VIEW_METAS = {
  jobs: { title: 'Agent Jobs', icon: Zap },
  memory: { title: 'Contextual Memory', icon: Brain },
  activity: { title: 'System Activity', icon: Activity },
  permissions: { title: 'Safety & Permissions', icon: Shield },
  tools: { title: 'Tool Registry', icon: Wrench },
  settings: { title: 'Settings & Environment', icon: Settings }
};

export default function SecondaryViews({
  viewId,
  onToggleDrawer,
  isDrawerOpen,
  onRunInChat
}) {
  const meta = VIEW_METAS[viewId] || { title: viewId, icon: Settings };
  const Icon = meta.icon;

  return (
    <div className="secondary-view-container">
      {/* Top Bar with Brand Trigger */}
      <div className="secondary-top-bar">
        <BrandTrigger onToggleDrawer={onToggleDrawer} isDrawerOpen={isDrawerOpen} />
        <div className="secondary-top-view-title">
          <Icon size={16} className="title-icon" />
          <span>{meta.title}</span>
        </div>
      </div>

      {/* View Component Dispatch */}
      <div className="secondary-content-body">
        {viewId === 'jobs' && <AgentJobsView />}
        {viewId === 'memory' && <MemoryView />}
        {viewId === 'activity' && <ActivityView />}
        {viewId === 'permissions' && <PermissionsView />}
        {viewId === 'tools' && <ToolsView onRunInChat={onRunInChat} />}
        {viewId === 'settings' && <SettingsView />}
      </div>
    </div>
  );
}
