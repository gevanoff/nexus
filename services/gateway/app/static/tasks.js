async function handleEditTask() {
  const taskId = state.selectedId;
  if (!taskId) return;

  const response = await fetch('/edit-task', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: taskId })
  });

  if (response.ok) {
    await fetchTasks();
    closeTaskEditModal();
  } else {
    setStatus('Failed to edit task', true);
  }
}

// Existing code continues...