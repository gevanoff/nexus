app.post('/api/tasks/edit', async (req, res) => {
  // Existing edit logic
  const { taskId, ...updates } = req.body;
  const updatedTask = await Task.findByIdAndUpdate(taskId, updates, { new: true });
  res.json({ success: true, task: updatedTask });

  console.log('Edit task request received:', req.body);
  try {
    const { taskId, ...updates } = req.body;
    // Example: Update task in a database
    const updatedTask = await Task.findByIdAndUpdate(taskId, updates, { new: true });
    res.json({ success: true, task: updatedTask });
  } catch (error) {
    res.status(500).json({ error: 'Failed to edit task' });
  }
});
