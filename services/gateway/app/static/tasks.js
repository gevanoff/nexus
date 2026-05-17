async function handleEditTask() {
  document.getElementById('edit-task-btn')?.addEventListener('click', handleEditTask);

  document.getElementById('edit-task-modal').style.display = 'block';
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
  document.getElementById('edit-button').addEventListener('click', async () => {
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
  });
  document.getElementById('edit-button').addEventListener('click', handleEditTask);
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