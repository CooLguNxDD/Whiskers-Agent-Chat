/** Panel kit: Unity-style dockable, resizable panels. See docs/panels.md. */
export { DockArea, zoneAt, type DockAreaProps, type PanelDef } from "./DockArea"
export { ResizeHandle, type ResizeHandleProps } from "./ResizeHandle"
export {
  createDockState, dockHome, resetAllDocks, readDockNumber, writeDockNumber, DOCK_STORAGE_PREFIX,
  type DockState, type PanelHome,
} from "./dockState"
export * from "./layout"
export { CollapsibleList, type CollapsibleListProps } from "./CollapsibleList"
